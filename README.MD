# Overview
This repository contains code that compares police shootings and killings from the OpenPoliceData (OPD) Python library to those from [Mapping Police Violence](https://mappingpoliceviolence.org/). The objective is to provide a tool that can be run periodically to check if any cases available in the latest OPD data are not in Mapping Police Violence.

Matches are based on demographics, date, location, and/or name. Logic has been added to account for errors and other differences between datasets. We have checked the current results by inspection and internet searches to ensure that the majority of cases found are not in Mapping Police Violence.

Currently, this tool is implemented for Mapping Police Violence, but we intend to update it in the future to handle the [Washington Post Police Shootings Database](https://www.washingtonpost.com/graphics/investigations/police-shootings-database/) and [Fatal Encounters](https://fatalencounters.org/).

# Installation
## Access the Code
[Clone](https://docs.github.com/en/repositories/creating-and-managing-repositories/cloning-a-repository) the OPD Police Shootings repository with [Git](https://git-scm.com/):

```
> git clone git@github.com:openpolicedata/police-shootings.git
```

OR

Download the OpenPoliceData Police Shootings repository [here](https://github.com/openpolicedata/police-shootings/archive/refs/heads/main.zip).

## Setup the Environment
Navigate to the police-shootings folder in a command prompt and run:
```
pip install -r requirements.txt
```

# Running the code
A script for comparing OPD datasets to the Mapping Police Violence database is located in [src\mapping_police_violence_update.py](https://github.com/openpolicedata/police-shootings/blob/main/src/mapping_police_violence_update.py). The code contains parameters at the top of the file that can be updated as needed. 

When the code is run, it will compare OPD data against Mapping Police Violence's data. It will report on cases identified as not being in Mapping Police Violence AND that have not been reported on previously. Thus, this tool can be run periodically to check if the most recent cases added to OPD datasets are not in Mapping Police Violence. To enable this feature, it is important to not delete past output from the tool and to always output to the same folder.

First-time use and subsequent uses can be considered different cases. The first time that the tool is run, it will find the most potential cases because it will be searching all historical data. Subsequent usage of the tool is likely to find a limited number of cases, if any, as only the newest cases from OPD will potentially be reported (if they are not in Mapping Police Violence).

# Interpreting Results
This tool outputs 3 types of files:
1. **Dataset-specific files**: these files contain all the fields from a single OPD dataset for each case indentified as not being in Mapping Police Violence. File is only generated if at least 1 case is found for the dataset. Example: Philadelphia_Pennsylvania_OFFICER-INVOLVED SHOOTINGS_MULTIPLE_20240629.csv for cases from Philadelphia's OIS data from running the tool on 29 June 2024.
2. **Global file**: this file contains general information for all cases from all datasets indentified as not being in Mapping Police Violence. File is only generated if at least 1 case is found. Example: Potential_MPV_Updates_Global_20240629.csv
3. **Possible matches file**: For each case found, this file contains information about the case as well as information about all cases in the state within a few days of the case and that have the same day and year but is off by 1 month (a frequently observed error). The OPD ID in the global file can be used to search for a specific case. This file can be used to identify cases that should be matches but no matches were found due to major (possibly erroneous) differences in the data. Example: Possible_Matches_20240629.txt

# Help
Please email us [here](mailto:openpolicedata@gmail.com) if you have any questions, issues, or recommendations. We are happy to help get you started working with this code.