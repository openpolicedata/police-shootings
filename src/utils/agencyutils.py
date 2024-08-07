import re
from .tools import split_words
from openpolicedata.defs import states
import pandas as pd
from typing import Literal, Optional

agency_words = ['Area','Rapid','Transit', 'Police', 'Department','Crisis', "Sheriff", 
                        'Township', 'Bureau', 'State', 'University', 'Public', 'Safety',
                        'Housing']
agency_types = ['Area Rapid Transit Police Department','Police Department', 'Crisis Response Team', "Sheriff's Office", 
                'Township Police Department', "Sheriff's Department", "Sheriff's Dept.",
                'Police Bureau', 'State University Department of Public Safety',
                'Housing Authority Police Department','Marshal Service',
                'Drug Enforcement Administration','Probation Department','Highway Patrol',
                "District Attorney's Office", 'State Police']
agency_types = sorted(list(agency_types), key=len, reverse=True)  # Sort from longest to shortest

def state_equals(s1, s2):
    if s1==s2:
        return True
    elif s1 in states:
        return states[s1]==s2
    elif s2 in states:
        return states[s2]==s1
    return False

def state_reverse(s):
    if s in states:
        return states[s]
    else:
        return [k for k,v in states.items() if v.lower()==s.lower()][0]
    
def full_state_name(s):
    if s in states:
        return s
    else:
        return state_reverse(s)
    
def state_abbrev(s):
    if s in states:
        return states[s]
    else:
        return s

_p_dept = re.compile('^department of .+', re.IGNORECASE)
def split(agency: str, state:str, unknown_type:Literal["ignore",'raise']='ignore'):
    """Split full agency name into location name and type (Such as "New York" and "Police Department" for "New York Police Department")

    Parameters
    ----------
    agency : str
        Full name of agency
    state : str
        State where agency is located
    unknown_type : str, optional
        How to handle cases where agency type cannot be determined:
            'ignore': Return full agency name as the partial name and an empty string for the type
            'raise': Throw an error,
            by default 'ignore'

    Returns
    -------
    _type_
        _description_
    """
    unknown_type = unknown_type.lower()
    assert unknown_type in ['ignore','raise']
    types_in_agency = [x for x in agency_types if agency.lower().endswith(x.lower())]
    if len(types_in_agency)==0:
        if _p_dept.search(agency):
            types_in_agency = [agency]
        elif agency.lower().startswith(full_state_name(state).lower()+" "):
            types_in_agency = [x for x in agency_types if x.lower() in agency.lower()]
            if len(types_in_agency)==0:
                if unknown_type=='raise':
                    raise ValueError(f"Unable to find agency type in {agency}")
                else:
                    return agency, ""
            idx = agency.lower().find(types_in_agency[0].lower())
            agency = agency[:idx+len(types_in_agency[0])]
            types_in_agency = ['']
        elif agency.lower().startswith(state_abbrev(state).lower()+" "):
            types_in_agency = [x for x in agency_types if x.lower() in agency.lower()]
            if len(types_in_agency)==0:
                if unknown_type=='raise':
                    raise ValueError(f"Unable to find agency type in {agency}")
                else:
                    return agency, ""
            agency = re.sub('^'+state_abbrev(state)+" ", full_state_name(state)+" ", agency, re.IGNORECASE)
            idx = agency.lower().find(types_in_agency[0].lower())
            agency = agency[:idx+len(types_in_agency[0])]
            types_in_agency = ['']
        elif unknown_type=='raise':
            raise ValueError(f"Unable to find agency type in {agency}")
        else:
            return agency, ""
    
    # Use longest type found
    agency_partial = agency.replace(types_in_agency[0],'').strip()

    return agency_partial, types_in_agency[0]

def filter_state(df, state_col, state):
    return df[df[state_col].apply(state_equals, args=(state,))]

def filter_agency(agency: str, agency_partial:str, agency_type:str, state:str, 
                  df: pd.DataFrame, agency_col:str, state_col:str, 
                  merge_county: bool, county_col: str,
                  cross_ref_agencies: Optional[dict] = None,
                  delim:str=',', exact:bool=False, logger=None, error='ignore'):
    """Filter table for rows likely corresponding to an agency

    Parameters
    ----------
    agency : str
        Full agency name
    agency_partial : str
        Partial agency name corresponding to location
    agency_type : str
        Partial agency name corresponding to agency type (i.e. Police Department)
    state : str
        State of agency
    df : pd.DataFrame
        Table to filter
    agency_col : str
        Agency column of table
    state_col : str
        State column of table
    merge_county : bool
        If true, will also include other parts of county in DataFrame that is returned
    county_col : str
        County column of table
    cross_ref_agencies : dict, optional
        Dictionary of data agencies keys that map to other agencies (str, list) contained in that agency's data
    delim : str, optional
        Delimiter separating agencies for rows containing multiple agencies, by default ','
    exact : bool, optional
        Whether an exact matching to agency or agency_partial is required or not, by default False
    logger : _type_, optional
        Python logger, by default None

    Returns
    -------
    pd.DataFrame
        Filtered table
    """

    assert error in ['raise','ignore']

    agency = agency.lower().strip().replace("&", 'and')
    agency_partial = agency_partial.lower().strip().replace("&", 'and')
    agency_type = agency_type.lower().strip().replace("&", 'and')
    
    agencies_all = df[agency_col].str.lower().str.replace("&", 'and')
    agencies_all = agencies_all.str.replace("^"+state_abbrev(state).lower()+" ", full_state_name(state).lower()+" ", regex=True)

    if merge_county:
        agency_partial = re.sub(r'\scounty\b','',agency_partial)
    
    agency_matches = (agencies_all.apply(str).str.contains(agency_partial)) & \
        df[state_col].apply(state_equals, args=(state,))
        
    df_agency = df[agency_matches]
    agencies_comp = agencies_all[agency_matches]

    if len(agency_partial)==0:
        agency_matches = agencies_comp.str.endswith(agency)
        agency_matches = agencies_comp[agency_matches].str.startswith(state.lower()) | \
            agencies_comp[agency_matches].str.startswith(state_reverse(state).lower())
        keep = list(agency_matches[agency_matches].index)
        if error=='raise' and len(keep)==0:
            raise NotImplementedError()
    else:
        keep = []
        words = split_words(agency_partial)
        for j, agency_val in agencies_comp.items():
            agency_val = agency_val.lower()
            if agency_val not in [agency,agency_partial]:
            
                # Agency can be a comma-separated list of multiple agencies
                agencies_check = agency_val
                agencies_check = agencies_check[1:] if agencies_check[0]=='"' else agencies_check
                agencies_check = agencies_check[:-1] if agencies_check[-1]=='"' else agencies_check
                agencies_check = [y.strip() for y in agencies_check.split(delim)]
                
                for a in agencies_check:
                    if a in [agency,agency_partial] or \
                        (not exact and len(m:=split_words(a))>=len(words) and words==m[:len(words)]):
                        break
                else:
                    continue
            keep.append(j)

    if len(keep)==0 and logger:
        logger.debug(f"No MPV shootings found for {agency}")

    df_agency = df_agency.loc[keep]

    if merge_county and county_col and len(df_agency)>0:
        df_county = df[(df[county_col]==df_agency[county_col].mode().iloc[0]) & (df[state_col]==df_agency[state_col].mode().iloc[0])]
        df_agency = pd.concat([df_agency, df_county])
        df_agency = df_agency[~df_agency.index.duplicated()]

    if cross_ref_agencies and any(key:=[x for x in cross_ref_agencies.keys() if agency_partial in x.lower()]):
         cross_refs = cross_ref_agencies[key[0]]
         cross_refs = [cross_refs] if isinstance(cross_refs, str) else cross_refs
         for c in cross_refs:
            agency_matches = (agencies_all.apply(str).str.contains(c.lower())) & \
                df[state_col].apply(state_equals, args=(state,))
            df_agency = pd.concat([df_agency, df[agency_matches]])
            df_agency = df_agency[~df_agency.index.duplicated()]

    return df_agency
