# Overview
This folder contains code that compares police shootings and killings from the OpenPoliceData (OPD) Python library to those from [Mapping Police Violence](https://mappingpoliceviolence.org/). The objective is to find historical cases available in an OPD dataset that may not be in the databases or to provide an additional method for finding recent shootings. 

Matches are based on demographics, date, location (address or zip code), and/or name. Logic has been added to account for errors in the data and other differences between datasets. We have checked the current results by inspection and internet searches to ensure that the majority of cases found do not appear to be in Mapping Police Violence.

Currently, it is implemented for Mapping Police Violence, but we intend to update it in the future to handle the [Washington Post Police Shootings Database](https://www.washingtonpost.com/graphics/investigations/police-shootings-database/) and [Fatal Encounters](https://fatalencounters.org/).

# Installation
## Access the Code
[Clone](https://docs.github.com/en/repositories/creating-and-managing-repositories/cloning-a-repository) the OPD Examples repository with [Git](https://git-scm.com/):

```
> git clone git@github.com:openpolicedata/police-shootings.git
```

OR

Download the OpenPoliceData police-shootings repository [here](https://github.com/openpolicedata/police-shootings/archive/refs/heads/main.zip).

## Setup the Environment
Navigate to the police-shootings folder in a command prompt and run:
```
pip install -r requirements.txt
```

# Running the code
A script for comparing OPD to the shootings databases is located in [src\mapping_police_violence_update.py](https://github.com/openpolicedata/police-shootings/blob/main/src/mapping_police_violence_update.py). The code contains parameters at the top of the file that can be updated as needed. 

For each dataset where unique cases are found, a CSV output file is created for that OPD dataset with all values for each case from the dataset. A set of general information for each case from all OPD datasets will be stored in Potential_MPV_Updates_Global_{Current Date}.csv. All files are datestamped so if the code is run on different days, new files will be generated. If the code is run multiple times, only cases that have not previously been found will be stored. Therefore, it is possible to rerun the code at a future date to get updates, which will be stored in a new datestamped file.

# Help
Please email us [here](mailto:openpolicedata@gmail.com) if you have any questions, issues, or recommendations. We are happy to help get you started working with this code.