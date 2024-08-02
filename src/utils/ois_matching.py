from datetime import timedelta
from rapidfuzz import fuzz
from itertools import product
from dataclasses import dataclass
import logging
import math
import numbers
import openpolicedata as opd
import pandas as pd
import re
from typing import Literal, Optional
import warnings

from . import address_parser
from . import agencyutils
from .tools import split_words

# Suppressing known warnings
warnings.filterwarnings(action='ignore', category=UserWarning, message='Identified difference in column names when combining sheets')
warnings.filterwarnings(action='ignore', category=UserWarning, message='Original table is a geopandas DataFrame')
warnings.filterwarnings(action='ignore', category=UserWarning, message="Column .* in current DataFrame does not match")
warnings.filterwarnings(action='ignore', category=UserWarning, message='Multiple potential .* columns')
warnings.filterwarnings(action='ignore', category=FutureWarning)

# Columns will be standardized below to use these names
date_col = opd.Column.DATE
agency_col = opd.Column.AGENCY
fatal_col = opd.Column.FATAL_SUBJECT
role_col = opd.Column.SUBJECT_OR_OFFICER
injury_cols = [opd.Column.INJURY_SUBJECT, opd.Column.INJURY_OFFICER_SUBJECT]
zip_col = opd.Column.ZIP_CODE

# Race values will be standardized below to use these values
race_cats = opd.defs.get_race_cats()
# Only keep specific values for testing later
[race_cats.pop(k) for k in ["MULTIPLE","OTHER","OTHER / UNKNOWN", "UNKNOWN", "UNSPECIFIED"]]
race_vals = race_cats.values()

# Gender values will be standardized below to use these values
gender_vals = opd.defs.get_gender_cats()
# Only keep specific values for testing later
[gender_vals.pop(k) for k in ["MULTIPLE","OTHER","OTHER / UNKNOWN", "UNKNOWN", "UNSPECIFIED"] if k in gender_vals]
gender_vals = gender_vals.values()

@dataclass
class Date_Matcher:
    max_diff: Optional[str] = None # If None, dates must match. Otherwise, dates will match if difference between dates is within this value
    allow_month_error: bool = False  # If True, dates will be also be considered to match if year and day are the same but the month is off by 1 (perhaps due to typo)

    def match(self, dates, date_comp):
        if self.max_diff:
            test = in_date_range(dates, date_comp, self.max_diff)
        else:
            test = match_date(dates, date_comp)

        if self.allow_month_error and not isinstance(dates.dtype, pd.api.types.PeriodDtype): 
            if pd.api.types.is_datetime64_any_dtype(dates):
                year_matches = dates.dt.year == date_comp.year
                day_matches = dates.dt.day == date_comp.day
                month_matches = abs(dates.dt.month - date_comp.month)==1
            else:
                year_matches = dates.apply(lambda x: x.year==date_comp.year if not isinstance(x,pd.Period) else False)
                day_matches  = dates.apply(lambda x: x.day==date_comp.day if not isinstance(x,pd.Period) else False)
                month_matches  = dates.apply(lambda x: abs(x.month-date_comp.month)==1 if not isinstance(x,pd.Period) else False)

            test = test | (year_matches & day_matches & month_matches)

        return test


@dataclass
class OIS_Matcher:
    df_mpv_agency: pd.DataFrame  # Table of MPV data for agency corresponding to df_opd
    mpv_addr_col: str            # Address column in MPV data
    addr_col: str                # Address column in OPD data
    mpv_state_col: str           # State column in MPV data
    location: Optional[str] = None  # Name of location (if known).
    # Whether to throw an error if previously unobserved conditions are found
    error: Literal['raise','ignore'] = 'ignore'

    def remove_name_matches(self,
                            df_opd: pd.DataFrame, 
                            mpv_matched: pd.Series,
                            state: str = None,
                            df_all: pd.DataFrame = None):
        """Loop over each row of df_mpv_agency to find cases in df_opd for the same date and matching demographics

        Parameters
        ----------
        df_opd : pd.DataFrame
            OPD table to match with MPV data
        mpv_matched : pd.Series
            Series indicating which MPV rows have previously been matched
        state: str
            State for agency corresponding to df_opd
        df_all: pd.DataFrame
            Table of MPV data

        Returns
        -------
        Returns updated versions of df_opd, mpv_matched
        """

        if opd.Column.NAME_SUBJECT in df_opd:
            col1 = opd.Column.NAME_SUBJECT
        elif opd.Column.NAME_OFFICER_SUBJECT in df_opd:
            col1 = opd.Column.NAME_OFFICER_SUBJECT
        else:
            return df_opd, mpv_matched

        if opd.Column.NAME_SUBJECT in self.df_mpv_agency:
            col2 = opd.Column.NAME_SUBJECT
        elif opd.Column.NAME_OFFICER_SUBJECT in self.df_mpv_agency:
            col2 = opd.Column.NAME_OFFICER_SUBJECT
        else:
            return df_opd, mpv_matched
        
        if state:
            # Check for cases where shooting might be listed under another agency or MPV agency might be null
            mpv_state = agencyutils.filter_state(df_all, self.mpv_state_col, state)
            # Remove cases that have already been checked
            df_mpv = mpv_state.drop(index=self.df_mpv_agency.index)
            mpv_matched = pd.Series(False, self.df_mpv_agency.index)
        else:
            df_mpv = self.df_mpv_agency.copy()

        thresh = 70

        def clean_name(x):
            return x.replace("'",'').replace('-',' ').replace(',',' ').replace('.',' ').replace('  ',' ').strip()
        
        rcol1 = get_race_col(df_opd)
        rcol2 = get_race_col(df_mpv)

        acol1 = get_age_col(df_opd)
        acol2 = get_age_col(df_mpv)
        
        keep = pd.Series(True, index=df_opd.index)
        for idx, name in df_opd[col1].items():
            cname = clean_name(name)
            names_set = set(cname.split(" "))
            db_names = df_mpv[col2][~mpv_matched]
            scores = db_names.apply(lambda x: fuzz.token_sort_ratio(cname, clean_name(x)))
            num_common = db_names.apply(lambda x: len(names_set.intersection(clean_name(x).split(' '))))

            if (exceeds:=(scores>=thresh) | ((scores>0.6) & (num_common>=2))).any():
                dates = df_mpv.loc[scores[exceeds].index, date_col]
                year_matches = dates.dt.year == df_opd.loc[idx, date_col].year
                day_matches = dates.dt.day == df_opd.loc[idx, date_col].day

                m = (dates - df_opd.loc[idx, date_col]).abs() <= '1d'
                m = m | (year_matches & day_matches & (
                        (dates.dt.month == df_opd.loc[idx, date_col].month) | 
                        abs(dates.dt.month - df_opd.loc[idx, date_col].month)==1   # Likely typo in month
                        ))
                if m.any():
                    if m.sum()>1 and self.error=='raise':
                        raise ValueError(f"Multiple name matches: {cname} vs {df_mpv.loc[scores[exceeds].index, col2]}")
                    else:
                        keep[idx] = False
                        mpv_matched[m[m].index[0]] = True
                elif self.error=='raise':
                    raise ValueError(f"Dates ({df_opd.loc[idx, date_col]} vs {dates}) do not match for "+
                                    f"{cname} vs {df_mpv.loc[scores[exceeds].index, col2]}")
                else:
                    # Name match but not a date match. Just mark a match if self.error!='raise'
                    keep[idx] = False
                    mpv_matched[scores[scores==scores.max()].index[0]] = True
            elif (m1:= df_mpv.loc[~mpv_matched, date_col]==df_opd.loc[idx, date_col]).any() and \
                (m2:=df_mpv[~mpv_matched][m1][col2].apply(lambda x: any([y in split_words(cname) for y in split_words(x)]))).any():
                # Same date and part of name is common
                if m2.sum()>1 and self.error=='raise':
                    raise ValueError(f"Multiple name matches: {cname} vs {df_mpv[~mpv_matched][m1][m2][col2]}")
                
                keep[idx] = False
                mpv_matched[df_mpv[~mpv_matched][m1][m2].index[0]] = True
            elif (m1 := (abs(df_mpv.loc[~mpv_matched, date_col] - df_opd.loc[idx, date_col]) <= '1d')).any() and \
                (m2:=df_mpv[~mpv_matched][m1][col2].apply(lambda x: x.lower() in ['name withheld',''])).any() and \
                rcol1 and acol1 and (m3:=df_opd.loc[idx, rcol1] == df_mpv[~mpv_matched][m1][m2][rcol2]).any():
                # Name was withheld but date is close and race is the same
                keep[idx] = False
                mpv_matched[df_mpv[~mpv_matched][m1][m2][m3].index[0]] = True
            elif self.error=='raise':
                dates = df_mpv.loc[~mpv_matched, date_col]
                assert (abs(dates - df_opd.loc[idx, date_col]) > '1d').all()

        return df_opd[keep], mpv_matched


    def remove_matches_date_match_first(self, 
                                    df_opd:pd.DataFrame, 
                                    mpv_matched: pd.Series, 
                                    subject_demo_correction: dict, 
                                    match_with_age_diff: dict, 
                                    args: list[dict],
                                    test_cols: list[str],
                                    date_matcher: Date_Matcher=Date_Matcher()):
        """Loop over each row of df_mpv_agency to find cases in df_opd for the same date and matching demographics

        Parameters
        ----------
        df_opd : pd.DataFrame
            OPD table to match with MPV data
        mpv_matched : pd.Series
            Series indicating which MPV rows have previously been matched
        subject_demo_correction : dict
            Dictionary mapping MPV rows to OPD rows for the same case but which have race and/or gender differences
        match_with_age_diff : dict
            Dictionary mapping MPV rows to OPD rows for the same case but which have age differences
        args : list[dict]
            Keyword arguments that are passed to check_for_match to control how strict a match is required for demographics.
            See check_for_match for what keyword arguments are available and their defintions
        test_cols : list[str]
            List of columns to use when looking for duplicate rows in df_opd
        date_matcher : Date_Matcher
            Date_Matcher object that describes how close the dates must be to be a match

        Returns
        -------
        Returns updated versions of df_opd, mpv_matched, subject_demo_correction, match_with_age_diff
        """

        assert self.error in ['raise','ignore']

        logger = logging.getLogger("ois")
        for j, row_mpv in self.df_mpv_agency.iterrows():
            if len(df_opd)==0:
                break

            df_matches = df_opd[date_matcher.match(df_opd[date_col], row_mpv[date_col])]

            if len(df_matches)==0:
                continue

            age_diff = pd.Series(False, index=df_matches.index)
            is_match, is_unknown, is_diff_race = check_for_match(df_matches, row_mpv,**args)
            if is_match.any():
                age_diff[is_match] = 'max_age_diff' in args.keys()
            else:
                logger.warning(f"Matching date found in OPD for {row_mpv[date_col]} but demographics do not match")
                continue

            if is_match.sum()>1:
                throw = True
                summary_col = [x for x in df_matches.columns if 'summary' in x.lower()]
                if self.addr_col:
                    test_cols_reduced = test_cols.copy()
                    if self.addr_col in test_cols_reduced:
                        test_cols_reduced.remove(self.addr_col)
                    for x in summary_col:
                        if x in test_cols_reduced:
                            test_cols_reduced.remove(x)
                    if len(drop_duplicates(df_matches[is_match], subset=test_cols_reduced, ignore_null=True, ignore_date_errors=True))==1:
                        # These are the same except for the address. Check if the addresses are similar
                        addr_match = street_match(df_matches[is_match][self.addr_col].iloc[0], self.addr_col, 
                                                            df_matches[is_match][self.addr_col].iloc[1:], notfound=self.error, location=self.location)
                        throw = not addr_match.all()
                    if throw:
                        #Check if address only matches one case
                        addr_match = street_match(row_mpv[self.mpv_addr_col], self.mpv_addr_col, df_matches[is_match][self.addr_col], location=self.location, notfound=self.error)
                        throw = addr_match.sum()!=1
                        if not throw:
                            is_match = addr_match
                        elif (date_type_match:=df_matches[is_match][addr_match][date_col].apply(lambda x: not isinstance(x, pd.Period))).sum()==1:
                            is_match = addr_match & date_type_match
                            throw = False
                elif zipcode_isequal(row_mpv, df_matches[is_match], count=1):
                # elif zip_col and zip_col and (m:=row_match[zip_col]==df_matches[is_match][zip_col]).sum()==1:
                    is_match.loc[is_match] = row_mpv[zip_col]==df_matches[is_match][zip_col]
                    throw = False
                if throw:
                    if len(summary_col)>0:
                        throw = not (df_matches[summary_col]==df_matches[summary_col].iloc[0]).all().all()
                if throw and self.error=='raise' and \
                    not (self.location=='Phoenix' and 'HUNDRED_BLOCK' in df_matches and df_matches['HUNDRED_BLOCK'].str.contains('65XX S 3RD ST').any()):
                    # The exception above is a known duplicate that is hard to ignore due to a repeated row with different addresses
                    raise NotImplementedError("Multiple matches found")
                    
            for idx in df_matches.index:
                # OIS found in data. Remove from df_test.
                if is_match[idx]: 
                    if is_unknown[idx] or is_diff_race[idx]:
                        subject_demo_correction[j] = df_matches.loc[idx]
                    if age_diff[idx]:
                        if j in match_with_age_diff:
                            raise ValueError("Attempting age diff id twice")
                        match_with_age_diff[j] = df_matches.loc[idx]

                    df_opd = df_opd.drop(index=idx)
                    mpv_matched[j] = True

        return df_opd, mpv_matched, subject_demo_correction, match_with_age_diff


    def remove_matches_demographics_match_first(self,
                                            df_opd: pd.DataFrame, 
                                            mpv_matched: pd.Series):
        """Loop over each row of df_mpv_agency to find cases in df_opd for the same demographics and close or matching date, address, and/or zip code

        Parameters
        ----------
        df_opd : pd.DataFrame
            OPD table to match with MPV data
        mpv_matched : pd.Series
            Series indicating which MPV rows have previously been matched

        Returns
        -------
        Returns updated versions of df_opd, mpv_matched
        """
        
        for j, row_match in self.df_mpv_agency.iterrows():
            if len(df_opd)==0:
                break
            if mpv_matched[j]:
                continue
            # Look for matches where dates differ
            is_match, _, _ = check_for_match(df_opd, row_match)

            if is_match.sum()>0:
                df_matches = df_opd[is_match]
                if len(df_matches)>1:
                    date_close = in_date_range(df_matches[date_col], row_match[date_col], '3d')
                    if self.addr_col:
                        addr_match = street_match(row_match[self.mpv_addr_col], self.mpv_addr_col, df_matches[self.addr_col], notfound=self.error, location=self.location)

                    if date_close.sum()==1 and (not self.addr_col or addr_match[date_close].iloc[0]):
                        df_opd = df_opd.drop(index=df_matches[date_close].index)
                        mpv_matched[j] = True
                    elif not self.addr_col and \
                        in_date_range(df_matches[date_col], row_match[date_col], min_delta='9d').all():
                        continue
                    elif self.addr_col and (not addr_match.any() or \
                        in_date_range(df_matches[addr_match][date_col], row_match[date_col], min_delta='300d').all()):
                        continue
                    elif self.error=='raise':
                        raise NotImplementedError()
                    else:
                        continue
                elif not self.addr_col:
                    if in_date_range(df_matches[date_col], row_match[date_col], '2d').iloc[0]:
                        df_opd = df_opd.drop(index=df_matches.index)
                        mpv_matched[j] = True
                    elif in_date_range(df_matches[date_col], row_match[date_col], '11d').iloc[0]:
                        if zipcode_isequal(row_match, df_matches, iloc2=0):
                            df_opd = df_opd.drop(index=df_matches.index)
                            mpv_matched[j] = True
                        elif zipcode_isequal(row_match, df_matches, iloc2=0, count='none'):
                            continue
                        elif self.error=='raise':
                            raise NotImplementedError()
                        else:
                            continue
                    elif in_date_range(df_matches[date_col],row_match[date_col], min_delta='30d').iloc[0]:
                        continue
                    elif zipcode_isequal(row_match, df_matches, iloc2=0, count='none'):
                        continue
                    elif self.error=='raise':
                        raise NotImplementedError()
                    else:
                        continue
                else:
                    date_very_close = in_date_range(df_matches[date_col], row_match[date_col], '1d').iloc[0]
                    date_close = in_date_range(df_matches[date_col], row_match[date_col], '3d').iloc[0]
                    addr_match = street_match(row_match[self.mpv_addr_col], self.mpv_addr_col, df_matches[self.addr_col], 
                                            notfound=self.error, location=self.location).iloc[0]
                    
                    if date_very_close or (date_close and addr_match):
                        df_opd = df_opd.drop(index=df_opd[is_match].index)
                        mpv_matched[j] = True
                    elif addr_match and in_date_range(df_matches[date_col], row_match[date_col], '31d', '30d').iloc[0]:
                        # Likely error in the month that was recorded
                        df_opd = df_opd.drop(index=df_opd[is_match].index)
                        mpv_matched[j] = True
                    elif self.error=='raise' and addr_match and ((df_matches[date_col]-row_match[date_col]).abs() < '32d').iloc[0]:
                        raise NotImplementedError()

        return df_opd, mpv_matched


    def remove_matches_street_match_first(self,
                                        df_opd: pd.DataFrame, 
                                        mpv_matched: pd.Series, 
                                        subject_demo_correction: dict):
        """Loop over each row of df_opd to find cases in df_mpv_agency for the same street and close date

        Parameters
        ----------
        df_opd : pd.DataFrame
            OPD table to match with MPV data
        mpv_matched : pd.Series
            Series indicating which MPV rows have previously been matched
        subject_demo_correction : dict
            Dictionary mapping MPV rows to OPD rows for the same case but which have race and/or gender differences

        Returns
        -------
        Returns updated versions of df_opd, mpv_matched, subject_demo_correction
        """
        
        j = 0
        while j<len(df_opd):
            if len(self.df_mpv_agency)==0:
                break
            
            mpv_unmatched = self.df_mpv_agency[~mpv_matched]

            matches = street_match(df_opd.iloc[j][self.addr_col], self.addr_col, mpv_unmatched[self.mpv_addr_col], notfound=self.error, location=self.location)

            if matches.any():
                date_close = in_date_range(df_opd.iloc[j][date_col], mpv_unmatched[matches][date_col], '3d')
                if date_close.any():
                    if self.error=='raise' and date_close.sum()>1:
                        raise NotImplementedError()
                    date_close = [k for k,x in date_close.items() if x][0]
                    # Consider this a match with errors in the demographics
                    if date_close in subject_demo_correction:
                        raise ValueError("Attempting demo correction twice")
                    subject_demo_correction[date_close] = df_opd.iloc[j]
                    df_opd = df_opd.drop(index=df_opd.index[j])
                    mpv_matched[date_close] = True
                    continue

            j+=1
        return df_opd, mpv_matched, subject_demo_correction


    def remove_matches_close_date_match_zipcode(self, 
                                            df_opd: pd.DataFrame, 
                                            mpv_matched: pd.Series, 
                                            match_with_age_diff, 
                                            allowed_replacements: dict={}):
        """Loop over each row of df_opd to find cases in df_mpv_agency for the same zip code and close date

        Parameters
        ----------
        df_opd : pd.DataFrame
            OPD table to match with MPV data
        mpv_matched : pd.Series
            Series indicating which MPV rows have previously been matched
        match_with_age_diff : dict
            Dictionary mapping MPV rows to OPD rows for the same case but which have age differences
        allowed_replacements : dict
            Dictionary contained values that are allowed to be interchanged. Keys of dictionary can be
            'race' (to indicate that the value contains race values that can be interchanged) or 'gender'.
            The value is a list of lists where the individual lists contain values that are to be considered equivalent
            (or to be acceptable differences). For example, if 
            allowed_replacements={'race',[["HISPANIC/LATINO","INDIGENOUS"],['ASIAN','ASIAN/PACIFIC ISLANDER']]}, 
            then, if the race of row_match was 'INDIGENOUS' and of a row of df was 'HISPANIC/LATINO', that would be
            considered a match (same with 'ASIAN' and 'ASIAN/PACIFIC ISLANDER'), by default {}

        Returns
        -------
        Returns updated versions of df_opd, mpv_matched, match_with_age_diff
        """

        test_gender_col = get_gender_col(df_opd)
        mpv_gender_col = get_gender_col(self.df_mpv_agency)

        j = 0
        while j<len(df_opd):
            if len(self.df_mpv_agency)==0 and mpv_matched.all():
                break
            
            mpv_unmatched = self.df_mpv_agency[~mpv_matched]

            date_close = in_date_range(df_opd.iloc[j][date_col], mpv_unmatched[date_col], '5d')
            if not date_close.any() or isinstance(df_opd.iloc[j][date_col], pd.Period):
                j+=1
                continue
            
            if zip_col in mpv_unmatched and zip_col in df_opd:
                if not (zip_matches:=mpv_unmatched[date_close][zip_col]==df_opd.iloc[j][zip_col]).any():
                    j+=1  # No zip codes match
                    continue
                date_diff = abs(mpv_unmatched[date_close][date_col] - df_opd.iloc[j][date_col])
                if (m:=(date_diff[zip_matches]<='4d')).any():  # Zip codes do match
                    is_match, _, _ = check_for_match(
                        mpv_unmatched[date_close][zip_matches][m], df_opd.iloc[j], 
                        max_age_diff=5, allowed_replacements=allowed_replacements)
                    if is_match.sum()==1:
                        match_with_age_diff[is_match[is_match].index[0]] = df_opd.iloc[j]
                        df_opd = df_opd.drop(index=df_opd.index[j])
                        mpv_matched[is_match[is_match].index[0]] = True
                        continue
                    elif test_gender_col in df_opd and df_opd.iloc[j][test_gender_col]=='FEMALE' and \
                        (mpv_unmatched[date_close][mpv_gender_col]=="MALE").all():
                        j+=1
                        continue
            else:
                j+=1        
            if self.error=='raise':
                raise NotImplementedError()

        return df_opd, mpv_matched, match_with_age_diff


    def remove_matches_agencymismatch(self,
                                      df_mpv: pd.DataFrame, 
                                    df_opd: pd.DataFrame, 
                                    state: str,
                                    match_type: Literal['address','zip'],
                                    allowed_replacements: dict = {'race':[['ASIAN','ASIAN/PACIFIC ISLANDER']]}):
        """Loop over each row of df_mpv to find cases where the agency does not match but the street or zipcode and demographics match
        and the date is close. 

        Parameters
        ----------
        df_mpv : pd.DataFrame
            Table of MPV data
        df_opd : pd.DataFrame
            OPD table to match with MPV data
        state: str
            State for agency corresponding to df_opd
        match_type: Literal['address','zip']
            Whether to match address or zip code
        allowed_replacements : dict, optional
            Dictionary contained values that are allowed to be interchanged. Keys of dictionary can be
            'race' (to indicate that the value contains race values that can be interchanged) or 'gender'.
            The value is a list of lists where the individual lists contain values that are to be considered equivalent
            (or to be acceptable differences). For example, if 
            allowed_replacements={'race',[["HISPANIC/LATINO","INDIGENOUS"],['ASIAN','ASIAN/PACIFIC ISLANDER']]}, 
            then, if the race of row_match was 'INDIGENOUS' and of a row of df was 'HISPANIC/LATINO', that would be
            considered a match (same with 'ASIAN' and 'ASIAN/PACIFIC ISLANDER'), by default {}

        Returns
        -------
        Returns updated versions of df_opd, mpv_matched, subject_demo_correction
        """

        match_type = match_type.lower()
        assert match_type in ['address','zip']
        if match_type=='address':
            assert self.mpv_addr_col and self.addr_col

        # Check for cases where shooting might be listed under another agency or MPV agency might be null
        mpv_state = agencyutils.filter_state(df_mpv, self.mpv_state_col, state)
        # Remove cases that have already been checked
        mpv_state = mpv_state.drop(index=self.df_mpv_agency.index)
        j = 0
        while j<len(df_opd):
            if match_type=='zip':
                addr_match = zipcode_isequal(df_opd, mpv_state, iloc1=j)
            else:
                addr_match = street_match(df_opd.iloc[j][self.addr_col], self.addr_col, mpv_state[self.mpv_addr_col], 
                                                        notfound=self.error, match_col_null=False,
                                                        location=self.location)
            if addr_match.any() and \
                in_date_range(df_opd.iloc[j][date_col], mpv_state[addr_match][date_col], '30d').any():
                if (m:=in_date_range(df_opd.iloc[j][date_col], mpv_state[addr_match][date_col], '1d')).any():
                    is_match, _, _ = check_for_match(mpv_state.loc[addr_match[addr_match][m].index], df_opd.iloc[j], allowed_replacements=allowed_replacements)
                    if is_match.any():
                        df_opd = df_opd.drop(index=df_opd.index[j])
                        continue
                    elif self.error=='raise':
                        raise NotImplementedError()
                elif self.error=='raise':
                    raise NotImplementedError()
                
            j+=1

        return df_opd

def zipcode_isequal(df1, df2, loc1=None, loc2=None, count=None, iloc1=None, iloc2=None):
    assert(count in [None,'all','any','none'] or isinstance(count, numbers.Number))
    
    if zip_col in df1 and zip_col in df2:
        if loc1:
            val1 = df1.loc[loc1, zip_col]
        elif isinstance(iloc1, numbers.Number):
            val1 = df1[zip_col].iloc[iloc1]
        else:
            val1 = df1[zip_col]
        if loc2:
            val2 = df2.loc[loc2, zip_col]
        elif isinstance(iloc2, numbers.Number):
            val2 = df2[zip_col].iloc[iloc2]
        else:
            val2 = df2[zip_col]

        matches = val1==val2

        is_series = isinstance(matches, pd.Series)
        if is_series and count=='all':
            return matches.all()
        elif is_series and count=='any':
            return matches.any()
        elif count=='none':
            if is_series:
                return not matches.any()
            else:
                return not matches
        elif isinstance(count, numbers.Number):
            return matches.sum()==count

        return matches
    if isinstance(df1, pd.DataFrame) and len(df1)>1 and iloc1==None and loc1==None:
        return pd.Series(False, index=df1.index)
    elif isinstance(df2, pd.DataFrame) and len(df2)>1 and iloc2==None and loc2==None:
        return pd.Series(False, index=df2.index)
    else:
        return False


def drop_duplicates(df, subset=None, ignore_null=False, ignore_date_errors=False):
    subset = subset if subset else df.columns
    if opd.Column.DATE in df and not isinstance(df[opd.Column.DATE].dtype, pd.PeriodDtype):
        df = df.copy()
        df[opd.Column.DATE] = df[opd.Column.DATE].apply(lambda x: x.replace(hour=0, minute=0, second=0) if not isinstance(x, pd.Period) else x)

    try:
        df = df.drop_duplicates(subset=subset, ignore_index=True)
        df_mod = df.copy()[subset]
    except TypeError as e:
        if len(e.args)>0 and e.args[0]=="unhashable type: 'dict'":
            df_mod = df.copy()
            df_mod = df_mod.apply(lambda x: x.apply(lambda y: str(y) if isinstance(y,dict) else y))
            dups = df_mod.duplicated(subset=subset)
            df = df[~dups].reset_index()
            df_mod = df_mod[~dups].reset_index()[subset]
        else:
            raise
    except:
        raise

    # Attempt cleanup and try again
    p = re.compile(r'\s?&\s?')
    df_mod = df_mod.apply(lambda x: x.apply(lambda x: p.sub(' and ',x).lower() if isinstance(x,str) else x))

    if ignore_date_errors:
        # Assume that if the full date matches, differences in other date-related fields should 
        # not block this from being a duplicate
        if any([x for x in subset if 'date' in x.lower()]):
            partial_date_terms = ['month','year','day','hour']
            reduced_subset = [x for x in subset if not any([y in x.lower() for y in partial_date_terms])]
            df_mod = df_mod[reduced_subset]

    dups = df_mod.duplicated()

    if ignore_null:
        # Assume that there are possibly multiple entries where some do no include all the information
        df_mod = df_mod.replace(opd.defs.UNSPECIFIED.lower(), pd.NA)
        for j in df_mod.index:
            for k in df_mod.index[j+1:]:
                if dups[j]:
                    break
                if dups[k]:
                    continue
                rows = df_mod.loc[[j,k]]
                nulls = rows.isnull().sum(axis=1)
                rows = rows.dropna(axis=1)
                if rows.duplicated(keep=False).all():
                    dups[nulls.idxmax()] = True

    df = df[~dups]

    return df


def _localize(dt):
    if is_series:=isinstance(dt, pd.Series):
        if not isinstance(dt.dtype, pd.api.types.PeriodDtype):
            if pd.api.types.is_object_dtype(dt):
                dt = dt.apply(lambda x: x.tz_localize(None) if not isinstance(x,pd.Period) else x)
            else:
                dt = dt.dt.tz_localize(None)
    elif isinstance(dt, pd.Timestamp):
        dt = dt.tz_localize(None)

    return dt, is_series

def _to_delta(x):
    if x[-1]=='d':
        return timedelta(days=int(x[:-1]))
    else:
        raise NotImplementedError()

def _date_comp_lt(dt1, dt2, max_delta):
    max_delta = _to_delta(max_delta)
    is_timestamp1 = not isinstance(dt1, pd.Period)
    is_timestamp2 = not isinstance(dt2, pd.Period)
    if not is_timestamp1 and not is_timestamp2:
        raise NotImplementedError()
    elif not is_timestamp2:
        return (((dt2.end_time >= dt1) & (dt2.start_time <= dt1)) | \
            (abs(dt2.end_time - dt1)<=max_delta) | (abs(dt2.start_time - dt1)<=max_delta))
    elif not is_timestamp1:
        return (((dt1.end_time >= dt2) & (dt1.start_time <= dt2)) | \
            (abs(dt1.end_time - dt2)<=max_delta) | (abs(dt1.start_time - dt2)<=max_delta))
    else:
        return (abs(dt1 - dt2)<=max_delta)
    
def _date_comp_gt(dt1, dt2, min_delta):
    min_delta = _to_delta(min_delta)
    is_timestamp1 = not isinstance(dt1, pd.Period)
    is_timestamp2 = not isinstance(dt2, pd.Period)
    if not is_timestamp1 and not is_timestamp2:
        raise NotImplementedError()
    elif not is_timestamp2:
        return (((dt2.end_time >= dt1) & (dt2.start_time <= dt1)) | \
            (abs(dt2.end_time - dt1)>=min_delta) | (abs(dt2.start_time - dt1)>=min_delta))
    elif not is_timestamp1:
        return (((dt1.end_time >= dt2) & (dt1.start_time <= dt2)) | \
            (abs(dt1.end_time - dt2)>=min_delta) | (abs(dt1.start_time - dt2)>=min_delta))
    else:
        return (abs(dt1 - dt2)>=min_delta)


def in_date_range(dt1, dt2, max_delta=None, min_delta=None):
    
    dt1, is_series1 = _localize(dt1)
    dt2, is_series2 = _localize(dt2)

    count1 = count2 = 1
    if isinstance(dt1, pd.Series):
        count1 = len(dt1)

    if isinstance(dt2, pd.Series):
        count2 = len(dt2)

    if count1!=count2 and not (count1==1 or count2==1):
        raise ValueError("Date inputs are different sizes")
    
    if isinstance(dt1, pd.Series) and count2==1:
        matches = pd.Series(True, index=dt1.index)
    elif isinstance(dt2, pd.Series):
        matches = pd.Series(True, index=dt2.index)
    else:
        matches = True

    if max_delta:
        if is_series1 and is_series2:
            matches = pd.Series([_date_comp_lt(x, y, max_delta) for x,y in zip(dt1, dt2)], index=dt2.index)
        elif is_series1:
            matches = dt1.apply(_date_comp_lt, args=(dt2, max_delta))
        elif is_series2:
            matches = dt2.apply(_date_comp_lt, args=(dt1, max_delta))
        else:
            matches = _date_comp_lt(dt1, dt2, max_delta)
        
    if min_delta:
        if is_series1 and is_series2:
            matches = matches & pd.Series([_date_comp_gt(x, y, min_delta) for x,y in zip(dt1, dt2)], index=dt2.index)
        elif is_series1:
            matches = matches & dt1.apply(_date_comp_gt, args=(dt2, min_delta))
        elif is_series2:
            matches = matches & dt2.apply(_date_comp_gt, args=(dt1, min_delta))
        else:
            matches = matches & _date_comp_gt(dt1, dt2, min_delta)

    return matches

def filter_by_date(df_test, date_col, min_date):
    if isinstance(df_test[date_col].dtype, pd.PeriodDtype):
        df_test = df_test[df_test[date_col].dt.start_time >= min_date]
    elif pd.api.types.is_object_dtype(df_test[date_col]):
        def comp(x):
            try:
                return x.tz_localize(None) >= min_date
            except:
                return x.start_time >= min_date
        df_test = df_test[df_test[date_col].apply(comp)]
    else:
        df_test = df_test[df_test[date_col].dt.tz_localize(None) >= min_date]
    return df_test


def match_date(dates, date_comp):
    date_comp = date_comp.replace(hour=0, minute=0, second=0)
    try:
        date_comp = date_comp.tz_localize(None)
    except:
        pass

    if isinstance(dates.dtype, pd.PeriodDtype):
        return (dates.dt.start_time <= date_comp) & (dates.dt.end_time >= date_comp)
    else:
        dates_test = dates.dt.tz_localize(None).apply(lambda x: x.replace(hour=0, minute=0, second=0))
        return dates_test == date_comp


def get_race_col(df):
    if opd.Column.RE_GROUP_SUBJECT in df:
        return opd.Column.RE_GROUP_SUBJECT
    elif opd.Column.RE_GROUP_OFFICER_SUBJECT in df:
        return opd.Column.RE_GROUP_OFFICER_SUBJECT 
    else:
        return None

def get_gender_col(df):
    if opd.Column.GENDER_SUBJECT in df:
        return opd.Column.GENDER_SUBJECT
    elif opd.Column.GENDER_OFFICER_SUBJECT in df:
        return opd.Column.GENDER_OFFICER_SUBJECT 
    else:
        return None

def get_age_col(df):
    if opd.Column.AGE_SUBJECT in df:
        return opd.Column.AGE_SUBJECT
    elif opd.Column.AGE_OFFICER_SUBJECT in df:
        return opd.Column.AGE_OFFICER_SUBJECT
    elif opd.Column.AGE_RANGE_SUBJECT in df:
        return opd.Column.AGE_RANGE_SUBJECT
    elif opd.Column.AGE_RANGE_OFFICER_SUBJECT in df:
        return opd.Column.AGE_RANGE_OFFICER_SUBJECT
    else:
        return None

def remove_officer_rows(df_test):
    # For columns with subject and officer data in separate rows, remove officer rows
    if role_col in df_test:
        df_test = df_test[df_test[role_col]==opd.defs.get_roles().SUBJECT]
    return df_test


_p_age_range = re.compile(r'^(\d+)\-(\d+)$')
def _compare_values(orig_val1, orig_val2, idx,
                    col1, col2, rcol1, gcol1, acol1, race_only_val1, race_only_val2,
                    is_unknown, is_match, is_diff_race,
                    allowed_replacements, check_race_only, inexact_age, max_age_diff,
                    allow_race_diff, delim1=',', delim2=',',
                    always_replace={race_cats[opd.defs._race_keys.ASIAN]:race_cats[opd.defs._race_keys.AAPI]}
                    ):
    # When we reach here, orig_val has already been tested to not equal db_val
    orig_val1 = orig_val1.split(delim1) if isinstance(orig_val1, str) and col1==rcol1 else [orig_val1]
    orig_val2 = orig_val2.split(delim2) if isinstance(orig_val2, str) and col1==rcol1 else [orig_val2]

    is_age_range1 = col1==acol1 and "RANGE" in col1
    is_age_range2 = col1==acol1 and "RANGE" in col2

    unknown_vals = ["UNKNOWN",'UNSPECIFIED','OTHER','PENDING RELEASE']
    other_found = False
    not_equal_found = False
    race_diff_found = False
    for val1, val2 in product(orig_val1, orig_val2):
        val1 = val1.strip() if isinstance(val1, str) else val1
        val1 = always_replace[val1] if val1 in always_replace else val1
        val2 = val2.strip() if isinstance(val2, str) else val2
        val2 = always_replace[val2] if val2 in always_replace else val2
        if is_age_range1 and is_age_range2:
            raise NotImplementedError()
        elif is_age_range1 or is_age_range2:
            if pd.isnull(val1) and pd.isnull(val2):
                return
            elif pd.isnull(val1):
                other_found = True
            elif (is_age_range1 and (m:=_p_age_range.search(val1))) or \
                 (is_age_range2 and (m:=_p_age_range.search(val2))):
                if pd.isnull(val2):
                    is_unknown[idx] = True  # db is unknown but val is not
                    return
                else:
                    other_val = val2 if is_age_range1 else val1
                    min_age = int(m.groups()[0])
                    max_age = int(m.groups()[1])
                    if min_age-max_age_diff <= other_val <= max_age+max_age_diff:
                        return  # In range
                    else:
                        not_equal_found = True
            else:
                raise NotImplementedError()
        elif (pd.isnull(val1) and pd.isnull(val2)) or \
            (pd.notnull(val1) and pd.notnull(val2) and val1==val2):
            return # Values are equal
        elif col2 in allowed_replacements and \
            any([val1 in x and val2 in x for x in allowed_replacements[col2]]):
            # Allow values in allowed_replacements to be swapped
            other_found = True
        elif col1==rcol1 and check_race_only and \
            (
                (race_only_val1 and race_only_val1==val2) or \
                (race_only_val2 and race_only_val2==val1) or \
                (race_only_val1 and race_only_val2 and race_only_val1==race_only_val2)
            ):
            return  # Race-only value matches db
        elif (pd.isnull(val2) or val2 in unknown_vals) and val1 not in unknown_vals:
            is_unknown[idx] = True  # db is unknown but val is not
            return
        elif col1==acol1 and isinstance(val2, numbers.Number) and isinstance(val1, numbers.Number) and \
            pd.notnull(val2) and pd.notnull(val1):
            if inexact_age:
                # Allow year in df_match to be an estimate of the decade so 30 would be a match for any value from 30-39
                is_match_cur = val1 == math.floor(val2/10)*10
            else:
                is_match_cur = abs(val1 - val2)<=max_age_diff
            if is_match_cur:
                is_match[idx] &= is_match_cur
                return
            not_equal_found = True
        elif col1==rcol1 and val1 in race_vals and val2 in race_vals:
            if allow_race_diff:
                race_diff_found = True
            else:
                not_equal_found = True
        elif col1 in [rcol1, gcol1] and (val1.upper() in unknown_vals or pd.isnull(val1)):
            other_found = True
        elif col1==gcol1 and val1 in gender_vals and val2 in gender_vals:
            not_equal_found = True
        elif col1==acol1 and pd.isnull(val1) and pd.notnull(val2):
            other_found = True
        else:
            raise NotImplementedError()
        
    if other_found:
        pass
    elif race_diff_found:
        is_diff_race[idx] = True
    elif not_equal_found:
        is_match[idx] = False
    else:
        raise NotImplementedError(f"{col1} not equal: OPD: {val1} vs. {val2}")


def check_for_match(df: pd.DataFrame, 
                    row_match: pd.Series, 
                    max_age_diff: int=0, 
                    allowed_replacements: dict={},
                    check_race_only: bool=False, 
                    inexact_age: bool=False, 
                    allow_race_diff: bool=False,
                    zip_match: bool=False):
    """Find rows of df that have matching demographics with row_match

    Parameters
    ----------
    df : pd.DataFrame
        Table to find matching demographics in
    row_match : pd.Series
        Series containing demographics values to match
    max_age_diff : int, optional
        Maximum allowed age difference, by default 0
    allowed_replacements : dict, optional
        Dictionary contained values that are allowed to be interchanged. Keys of dictionary can be
        'race' (to indicate that the value contains race values that can be interchanged) or 'gender'.
        The value is a list of lists where the individual lists contain values that are to be considered equivalent
        (or to be acceptable differences). For example, if 
        allowed_replacements={'race',[["HISPANIC/LATINO","INDIGENOUS"],['ASIAN','ASIAN/PACIFIC ISLANDER']]}, 
        then, if the race of row_match was 'INDIGENOUS' and of a row of df was 'HISPANIC/LATINO', that would be
        considered a match (same with 'ASIAN' and 'ASIAN/PACIFIC ISLANDER'), by default {}
    check_race_only : bool, optional
        If available, the race columns compared are the ones that combine race and ethnicity. If there is a race-only
        column in addition to that, it will be used instead if this flag is True, by default False
    inexact_age : bool, optional
        If True, the age will be a match if the age in df matches the decade of the age in row_match 
        (i.e. 30 would match any value from 30 to 39), by default False
    allow_race_diff : bool, optional
        If True, the race will be ignored when finding a match (only gender and age will be used), by default False
    zip_match : bool, optional
        If True, matches will require that df has a zip code column and that the zip code matches the one in row_match, by default False

    Returns
    -------
    pd.Series
        Boolean Series indicating whether each row of df matches row_match
    pd.Series
        Boolean Series indicating that for a match, there is a demographics column where MPV has an unknown value but df does not
    pd.Series
        Boolean Series indicating that for a match, the race is different

    """
    is_unknown = pd.Series(False, index=df.index)
    is_diff_race = pd.Series(False, index=df.index)
    is_match = pd.Series(True, index=df.index)

    race_only_col_df = opd.Column.RACE_SUBJECT if role_col not in df else opd.Column.RACE_OFFICER_SUBJECT
    rcol_df = get_race_col(df)
    gcol_df = get_gender_col(df)
    acol_df = get_age_col(df)

    rcol_row = get_race_col(row_match)
    gcol_row = get_gender_col(row_match)
    acol_row = get_age_col(row_match)
    race_only_col_row = opd.Column.RACE_SUBJECT if role_col not in row_match else opd.Column.RACE_OFFICER_SUBJECT

    if len(set(allowed_replacements.keys()) - {'race','gender'})>0:
        raise KeyError("Replacements only implemented for race and gener currently")
    if 'race' in allowed_replacements:
        allowed_replacements = allowed_replacements.copy()
        allowed_replacements[rcol_row] = allowed_replacements.pop('race')
    if 'gender' in allowed_replacements:
        allowed_replacements = allowed_replacements.copy()
        allowed_replacements[gcol_row] = allowed_replacements.pop('gender')

    for idx in df.index:
        if zip_match:
            if not (zipcode_isequal(df, row_match, loc1=idx)):
                is_match[idx] = False
        for col_df, col_row in zip([rcol_df, gcol_df, acol_df], [rcol_row, gcol_row, acol_row]):
            if col_df not in df or col_row not in row_match:
                continue
        
            _compare_values(df.loc[idx, col_df], row_match[col_row], idx,
                col_df, col_row, rcol_df, gcol_df, acol_df, 
                df.loc[idx, race_only_col_df] if race_only_col_df in df else None, 
                row_match[race_only_col_row] if race_only_col_row in row_match else None,
                is_unknown, is_match, is_diff_race,
                allowed_replacements, check_race_only, inexact_age, max_age_diff,
                allow_race_diff)
                
    return is_match, is_unknown, is_diff_race


def clean_data(opd_table: opd.data.Table, 
               df_opd: pd.DataFrame, 
               table_type: str,
               min_date: pd.Timestamp, 
               include_unknown_fatal: bool=False, 
               keep_self_inflicted: bool=False):
    """Clean data:
    1. Remove rows corresponding to officers instead of subjects
    2. Remove non-fatal cases
    3. Remove data prior to min_date
    4. Drop duplicate rows for the same subject

    Parameters
    ----------
    opd_table : opd.data.Table
        OPD Table object for the dataset
    df_opd : pd.DataFrame
        Table of data for the dataset
    table_type : str
        Type of dataset (OFFICER-INVOLVED SHOOTINGS, USE OF FORCE, etc.)
    min_date : pd.Timestamp
        Minimum date to keep
    include_unknown_fatal : bool, optional
        Whether to keep officer-involved shootings where the data does not indicate if the shooting was fatal, by default False
    keep_self_inflicted : bool, optional
        Whether to keep shootings that are self-inflicted, by default False

    Returns
    -------
    pd.DataFrame
        Clean version of df_opd
    bool
        Whether it is known that each case was fatal
    list[str]
        List of columns used to determine if rows are duplicates
    """
    # For data containing separate rows for officers and subjects, remove officers
    df_opd = remove_officer_rows(df_opd)

    # Filter data for cases that were fatal
    known_fatal = True
    if fatal_col in df_opd:
        fatal_values = ['YES',"UNSPECIFIED"] if include_unknown_fatal and "USE OF FORCE" not in table_type else ['YES']
        if keep_self_inflicted:
            fatal_values.append('SELF-INFLICTED FATAL')
        df_opd = df_opd[df_opd[fatal_col].isin(fatal_values)]
    elif len(c:=[x for x in injury_cols if x in df_opd])>0:
        df_opd = df_opd[df_opd[c[0]]=='FATAL']
    else:
        if not include_unknown_fatal or "USE OF FORCE" in table_type:
            return pd.DataFrame(columns=df_opd.columns), None, None
        known_fatal = False

    if len(df_opd)==0:
        return df_opd, None, None  # No data move to the next dataset
    
    df_opd = filter_by_date(df_opd.copy(), date_col, min_date=min_date)

    test_cols, ignore_cols = columns_for_duplicated_check(opd_table, df_opd)
    df_opd = drop_duplicates(df_opd, subset=test_cols)

    return df_opd, known_fatal, test_cols


def columns_for_duplicated_check(opd_table, df_matches_raw):
    # Select columns to use when checking for duplicated rows

    # Start with null values that may only be missing in some records
    ignore_cols = df_matches_raw.columns[df_matches_raw.isnull().any()].tolist()
    keep_cols = []
    for c in opd_table.get_transform_map():
        # Looping over list of columns that were standardized which provides old and new column names
        if c.new_column_name==opd.Column.INJURY_SUBJECT:
            # Same subject can have multiple injuries
            ignore_cols.append(c.new_column_name)
            ignore_cols.append("RAW_"+c.orig_column_name)
        elif "SUBJECT" in c.new_column_name:
            keep_cols.append(c.new_column_name)
        if "OFFICER" in c.new_column_name:
            # Officer columns may differ if multiple officers were involved
            if isinstance(c.orig_column_name,str):
                ignore_cols.append("RAW_"+c.orig_column_name)  # Original column gets renamed
            ignore_cols.append(c.new_column_name)
        elif c.new_column_name=="TIME":
            # Times can be different if person is shot multiple times
            ignore_cols.append(c.new_column_name)
            ignore_cols.append("RAW_"+c.orig_column_name)
        elif c.new_column_name=="DATETIME":
            ignore_cols.append(c.new_column_name)

    for c in df_matches_raw.columns:
        # Remove various other columns can differ between rows corresponding to the same individual
        notin = ["officer", "narrative", "objectid", "incnum", 'text', ' hash', 
                 'firearm','longitude','latitude','rank', 'globalid','rin',
                 'description','force','ofc','sworn','emp','weapon','shots','reason',
                 'perceived','armed','nature','level','number','resistance']
        if c not in ignore_cols and c not in keep_cols and \
            ("ID" in [x.upper() for x in split_words(c)] or c.lower().startswith("off") or \
             any([x in c.lower() for x in notin]) or c.lower().startswith('raw_')):
            ignore_cols.append(c)

    test_cols = [x for x in df_matches_raw.columns if x not in ignore_cols or x in keep_cols]
    return test_cols, ignore_cols


def match_street_word(x,y):
    if not (match:=x==y) and x[0].isdigit() and y[0].isdigit():
        # Handle cases such as matching 37th and 37
        match = (m:=re.search(r'^(\d+)[a-z]*$',x,re.IGNORECASE)) and \
                (n:=re.search(r'^(\d+)[a-z]*$',y,re.IGNORECASE)) and \
                m.group(1)==n.group(1)
    return match

def address_match(address1, address2, keys1=None, keys2=None, match_null=False):
    if pd.isnull(address1):
        return match_null
    if isinstance(address1,str):
        keys1 = ['key']
        address1 = {keys1[0]:address1}
    if pd.isnull(address2):
        return match_null
    if isinstance(address2,str):
        keys2 = ['key']
        address2 = {keys2[0]:address2}
    elif isinstance(address2,dict) and not keys2:
        return True
    for k1 in keys1:
        words1 = split_words(address1[k1].lower())
        for k2 in keys2:
            words2 = split_words(address2[k2].lower())
            for j,w in enumerate(words2[:len(words2)+1-len(words1)]):   # Indexing is to ensure that remaining words can be matched
                if match_street_word(w, words1[0]):
                    # Check that rest of word matches
                    for m in range(1, len(words1)):
                        if not match_street_word(words1[m], words2[j+m]):
                            break
                    else:
                        return True
    return False

def street_match(address, col_name, col, notfound='ignore', match_addr_null=False, match_col_null=True, location=None):
    assert notfound in ['raise', 'ignore']
    addr_tags, addr_type = address_parser.tag(address, location=location, col_name=col_name, error=notfound)
    
    matches = pd.Series(False, index=col.index, dtype='object')
    if pd.isnull(address):
        return matches
    if isinstance(addr_tags, list):
        for t in addr_tags:
            matches |= street_match(" ".join(t.values()), col_name, col, notfound, match_addr_null, match_col_null)
        return matches
    keys_check1 = [x for x in addr_tags.keys() if x.endswith('StreetName')]
    if len(keys_check1)==0:
        if notfound=='raise' and addr_type not in ['Coordinates','PlusCode','County','Region']:
            raise ValueError(f"'StreetName' not found in {address}")
        else:
            return pd.Series(match_addr_null, index=col.index, dtype='object')
    for idx in col.index:
        if not address_match(addr_tags, col[idx], keys1=keys_check1, match_null=match_col_null):
            continue
        ctags_all, ctype_all = address_parser.tag(col[idx], location, col.name, error=notfound)
        if not isinstance(ctags_all, list):
            ctags_all = [ctags_all]
            ctype_all = [ctype_all]
        for ctags, ctype in zip(ctags_all, ctype_all):
            keys_check2 = [x for x in ctags.keys() if x.endswith('StreetName')]
            if ctype in ['Null','Coordinates','Building','PlusCode']:
                if match_col_null:
                    matches[idx] = True
                continue
            if notfound=='raise' and len(keys_check2)==0:
                raise ValueError(f"'StreetName' not found in {col[idx]}")

            matches[idx] = address_match(addr_tags, ctags, keys1=keys_check1, keys2=keys_check2, match_null=match_col_null)
            if matches[idx]:
                break

    return matches

def get_logger(level):
    logger = logging.getLogger("ois")
    logger.setLevel(level)
    ch = logging.StreamHandler()
    ch.setLevel(level)
    logger.addHandler(ch)
    return logger
