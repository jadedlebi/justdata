"""
Utility to map counties to GEOID5 codes using BigQuery.
Uses state FIPS code + county FIPS code for precise matching via CBSA-to-county table.

GEOID5 structure: First 2 digits = state FIPS code, last 3 digits = county FIPS code
Example: 12057 = State 12 (Florida) + County 057 (Hillsborough) = Hillsborough County, Florida
"""

from typing import List, Dict, Tuple, Optional, Union
from justdata.shared.utils.bigquery_client import get_bigquery_client, execute_query
from justdata.apps.mergermeter.config import PROJECT_ID

# Connecticut eliminated county government in 1960 but federal data systems kept using
# county FIPS codes (09001-09015) until the 2024 activity year, when HMDA/CRA reporting
# switched to the state's 9 planning regions (09110-09190). NCRC's own BigQuery tables
# straddle both vintages: bizsight.sb_county_summary is legacy-county-coded through 2023
# and planning-region-coded from 2024 on, so a geoid5 filter list built from only one
# vintage silently drops or misses Connecticut for whichever years use the other vintage.
# Expanding every CT geoid to both vintages makes the filter list vintage-agnostic.
CT_PLANNING_REGION_TO_COUNTIES = {
    '09110': ['09003'],           # Capitol -> Hartford
    '09120': ['09001'],           # Greater Bridgeport -> Fairfield
    '09130': ['09007', '09011'],  # Lower Connecticut River Valley -> Middlesex, New London
    '09140': ['09009', '09005'],  # Naugatuck Valley -> New Haven, Litchfield
    '09150': ['09013', '09015'],  # Northeastern Connecticut -> Tolland, Windham
    '09160': ['09005'],           # Northwest Hills -> Litchfield
    '09170': ['09009', '09007'],  # South Central -> New Haven, Middlesex
    '09180': ['09011'],           # Southeastern Connecticut -> New London
    '09190': ['09001', '09005'],  # Western Connecticut -> Fairfield, Litchfield
}
CT_COUNTY_TO_PLANNING_REGIONS = {}
for _region, _counties in CT_PLANNING_REGION_TO_COUNTIES.items():
    for _county in _counties:
        CT_COUNTY_TO_PLANNING_REGIONS.setdefault(_county, []).append(_region)


def expand_connecticut_geoids(geoids: List[str]) -> List[str]:
    """
    Expand any Connecticut GEOID5 (state FIPS 09) to include its equivalent(s) in the
    other vintage, so a single filter list matches Connecticut data regardless of which
    vintage a given table uses. Non-Connecticut geoids pass through unchanged.
    """
    expanded = set()
    for geoid in geoids:
        geoid5 = str(geoid).zfill(5)
        expanded.add(geoid5)
        if geoid5 in CT_PLANNING_REGION_TO_COUNTIES:
            expanded.update(CT_PLANNING_REGION_TO_COUNTIES[geoid5])
        elif geoid5 in CT_COUNTY_TO_PLANNING_REGIONS:
            expanded.update(CT_COUNTY_TO_PLANNING_REGIONS[geoid5])
    return list(expanded)


def map_counties_to_geoids(
    counties: Union[List[str], List[Dict[str, str]]]
) -> Tuple[List[str], List[str]]:
    """
    Map counties to GEOID5 codes using state code + county code for precise matching.
    
    Accepts multiple input formats:
    1. List of strings: ["County, State"] format (e.g., "Hillsborough County, Florida")
    2. List of dicts: [{"state_code": "12", "county_code": "057"}] format (preferred)
    3. List of dicts: [{"state_fips": "12", "county_fips": "057"}] format
    4. List of dicts: [{"geoid5": "12057"}] format
    
    Args:
        counties: List of county identifiers (strings or dicts)
    
    Returns:
        Tuple of (mapped_geoids, unmapped_counties)
        - mapped_geoids: List of 5-digit GEOID5 strings (zero-padded)
        - unmapped_counties: List of county identifiers that couldn't be mapped
    """
    if not counties:
        return [], []
    
    # Parse input into standardized format
    county_identifiers = []
    for county in counties:
        if isinstance(county, dict):
            # Already in dict format - extract codes
            state_code = county.get('state_code') or county.get('state_fips') or ''
            county_code = county.get('county_code') or county.get('county_fips') or ''
            geoid5 = county.get('geoid5') or ''
            
            if geoid5:
                # Direct GEOID5 provided
                geoid5_str = str(geoid5).zfill(5)
                if len(geoid5_str) == 5:
                    county_identifiers.append({
                        'type': 'geoid5',
                        'geoid5': geoid5_str,
                        'state_code': geoid5_str[:2],
                        'county_code': geoid5_str[2:]
                    })
            elif state_code and county_code:
                # State + County codes provided
                state_code_str = str(state_code).zfill(2)
                county_code_str = str(county_code).zfill(3)
                geoid5_str = state_code_str + county_code_str
                county_identifiers.append({
                    'type': 'codes',
                    'geoid5': geoid5_str,
                    'state_code': state_code_str,
                    'county_code': county_code_str
                })
            else:
                # Try to extract from county name if present
                county_name = county.get('county_name') or county.get('county') or ''
                state_name = county.get('state_name') or county.get('state') or ''
                if county_name and state_name:
                    county_identifiers.append({
                        'type': 'name',
                        'county_name': county_name,
                        'state_name': state_name
                    })
        elif isinstance(county, str):
            # String format - parse "County, State" or try to extract codes
            if ', ' in county:
                parts = county.split(', ', 1)
                if len(parts) == 2:
                    county_identifiers.append({
                        'type': 'name',
                        'county_name': parts[0].strip(),
                        'state_name': parts[1].strip()
                    })
            else:
                # Might be a GEOID5 or state+county code format
                county_identifiers.append({
                    'type': 'name',
                    'county_name': county.strip(),
                    'state_name': ''
                })
    
    if not county_identifiers:
        return [], counties if isinstance(counties[0], str) else []
    
    # Separate by type for efficient querying
    geoid5_list = []
    code_pairs = []
    name_pairs = []
    
    for identifier in county_identifiers:
        if identifier['type'] == 'geoid5':
            geoid5_list.append(identifier['geoid5'])
        elif identifier['type'] == 'codes':
            code_pairs.append((identifier['state_code'], identifier['county_code']))
        elif identifier['type'] == 'name':
            name_pairs.append((identifier.get('county_name', ''), identifier.get('state_name', '')))
    
    # OPTIMIZATION: If all counties have GEOIDs or codes, skip BigQuery lookup entirely
    if geoid5_list and not name_pairs:
        # All counties have GEOIDs - return immediately (fastest path)
        # Convert code_pairs to GEOIDs
        for state_code, county_code in code_pairs:
            geoid5 = f"{state_code}{county_code}"
            if geoid5 not in geoid5_list:
                geoid5_list.append(geoid5)
        
        # Return unique GEOIDs (already validated as 5-digit)
        return list(set(geoid5_list)), []
    
    # If we have code pairs but no name lookups needed, convert codes to GEOIDs
    if code_pairs and not name_pairs:
        for state_code, county_code in code_pairs:
            geoid5 = f"{state_code}{county_code}"
            if geoid5 not in geoid5_list:
                geoid5_list.append(geoid5)
        return list(set(geoid5_list)), []
    
    # Build query conditions (only needed if we have name lookups)
    conditions = []
    
    # Direct GEOID5 matches (most precise)
    if geoid5_list:
        geoid5_str = ', '.join([f"'{g}'" for g in geoid5_list])
        conditions.append(f"CAST(geoid5 AS STRING) IN ({geoid5_str})")
    
    # State code + County code matches (precise)
    if code_pairs:
        code_conditions = []
        for state_code, county_code in code_pairs:
            # GEOID5 = state_code (2 digits) + county_code (3 digits)
            geoid5 = f"{state_code}{county_code}"
            code_conditions.append(f"CAST(geoid5 AS STRING) = '{geoid5}'")
        if code_conditions:
            conditions.append("(" + " OR ".join(code_conditions) + ")")
    
    # County name + State name matches (less precise, fallback)
    if name_pairs:
        name_conditions = []
        for county_name, state_name in name_pairs:
            if county_name and state_name:
                from justdata.shared.utils.bigquery_client import escape_sql_string
                county_escaped = escape_sql_string(county_name)
                state_escaped = escape_sql_string(state_name)
                name_conditions.append(f"(County = '{county_escaped}' AND State = '{state_escaped}')")
        if name_conditions:
            conditions.append("(" + " OR ".join(name_conditions) + ")")
    
    if not conditions:
        return [], counties if isinstance(counties[0], str) else []
    
    where_clause = " OR ".join(conditions)
    
    query = f"""
    SELECT DISTINCT
        CAST(geoid5 AS STRING) as geoid5,
        County as county_name,
        State as state_name,
        CONCAT(County, ', ', State) as county_state,
        SUBSTR(LPAD(CAST(geoid5 AS STRING), 5, '0'), 1, 2) as state_fips,
        SUBSTR(LPAD(CAST(geoid5 AS STRING), 5, '0'), 3, 3) as county_fips,
        CAST(cbsa_code AS STRING) as cbsa_code,
        CBSA as cbsa_name
    FROM `{PROJECT_ID}.shared.cbsa_to_county`
    WHERE {where_clause}
    """
    
    try:
        client = get_bigquery_client(PROJECT_ID, app_name='MERGERMETER')
        results = execute_query(client, query)

        # Create mapping
        mapped_geoids = []
        mapped_identifiers = set()
        
        for row in results:
            geoid5 = str(row.get('geoid5', '')).zfill(5)
            if geoid5 and len(geoid5) == 5:
                mapped_geoids.append(geoid5)
                # Track what was matched
                county_state = row.get('county_state', '')
                state_fips = row.get('state_fips', '')
                county_fips = row.get('county_fips', '')
                if county_state:
                    mapped_identifiers.add(county_state)
                if state_fips and county_fips:
                    mapped_identifiers.add(f"{state_fips}:{county_fips}")
                mapped_identifiers.add(geoid5)
        
        # Find unmapped counties
        unmapped_counties = []
        for county in counties:
            if isinstance(county, str):
                if county not in mapped_identifiers:
                    unmapped_counties.append(county)
            elif isinstance(county, dict):
                geoid5 = county.get('geoid5') or ''
                state_code = county.get('state_code') or county.get('state_fips') or ''
                county_code = county.get('county_code') or county.get('county_fips') or ''
                county_name = county.get('county_name') or county.get('county') or ''
                state_name = county.get('state_name') or county.get('state') or ''
                
                # Check if any identifier was matched
                matched = False
                if geoid5 and str(geoid5).zfill(5) in mapped_identifiers:
                    matched = True
                elif state_code and county_code and f"{str(state_code).zfill(2)}:{str(county_code).zfill(3)}" in mapped_identifiers:
                    matched = True
                elif county_name and state_name and f"{county_name}, {state_name}" in mapped_identifiers:
                    matched = True
                
                if not matched:
                    unmapped_counties.append(county)
        
        return list(set(mapped_geoids)), unmapped_counties
        
    except Exception as e:
        print(f"Error mapping counties to GEOIDs: {e}")
        import traceback
        traceback.print_exc()
        return [], counties if isinstance(counties[0], str) else []


def get_counties_by_msa_name(msa_name: str) -> Tuple[List[str], Optional[str]]:
    """
    Look up all counties within an MSA/CBSA by name.
    
    Args:
        msa_name: MSA/CBSA name (e.g., "Denver-Aurora-Lakewood, CO" or "Tampa-St. Petersburg-Clearwater, FL")
    
    Returns:
        Tuple of (counties, cbsa_code)
        - counties: List of county names in "County, State" format
        - cbsa_code: CBSA code if found, None otherwise
    """
    if not msa_name or not msa_name.strip():
        return [], None
    
    # Clean up MSA name - remove common suffixes and normalize
    import re
    clean_name = msa_name.strip()
    
    # Remove trailing "MSA" or "CBSA" if present
    clean_name = re.sub(r'\s+(MSA|CBSA)$', '', clean_name, flags=re.IGNORECASE).strip()
    
    # Try exact match first, then partial match
    try:
        client = get_bigquery_client(PROJECT_ID, app_name='MERGERMETER')

        # First try exact match
        query = f"""
        SELECT DISTINCT
            CAST(cbsa_code AS STRING) as cbsa_code,
            CBSA as cbsa_name,
            CONCAT(County, ', ', State) as county_state
        FROM `{PROJECT_ID}.shared.cbsa_to_county`
        WHERE UPPER(TRIM(CBSA)) = UPPER(TRIM('{clean_name.replace("'", "''")}'))
        ORDER BY county_state
        """
        
        results = execute_query(client, query)
        
        if not results:
            # Try partial match (contains)
            query = f"""
            SELECT DISTINCT
                CAST(cbsa_code AS STRING) as cbsa_code,
                CBSA as cbsa_name,
                CONCAT(County, ', ', State) as county_state
            FROM `{PROJECT_ID}.shared.cbsa_to_county`
            WHERE UPPER(TRIM(CBSA)) LIKE UPPER(TRIM('%{clean_name.replace("'", "''")}%'))
            ORDER BY cbsa_code, county_state
            """
            results = execute_query(client, query)
        
        if results:
            counties = []
            cbsa_code = None
            
            for row in results:
                county_state = row.get('county_state', '')
                if county_state:
                    counties.append(county_state)
                if not cbsa_code:
                    cbsa_code = row.get('cbsa_code', '')
            
            return list(set(counties)), cbsa_code
        
        return [], None
        
    except Exception as e:
        print(f"Error looking up MSA name '{msa_name}': {e}")
        import traceback
        traceback.print_exc()
        return [], None


def detect_and_expand_msa_names(input_list: List[str], progress_callback=None) -> List[str]:
    """
    Detect MSA names in a list and expand them to include all counties.
    
    Args:
        input_list: List of strings that may contain MSA names or county names
        progress_callback: Optional callback function(progress_data) for progress updates
    
    Returns:
        Expanded list with MSA names replaced by their counties
    """
    if not input_list:
        return []
    
    expanded_list = []
    
    total_items = len(input_list)
    processed = 0
    
    for item in input_list:
        # Skip dictionaries - they're already in the correct format
        if isinstance(item, dict):
            expanded_list.append(item)
            processed += 1
            continue
        
        # Only process strings
        if not isinstance(item, str):
            expanded_list.append(item)
            processed += 1
            continue
        
        item = item.strip()
        if not item:
            processed += 1
            continue
        
        # Check if this looks like an MSA name (not a county, state format)
        # MSA names typically don't have "County" in them and may have hyphens or commas
        # County names typically have "County, State" format
        
        # If it doesn't match "County, State" pattern, it might be an MSA name
        if ', ' not in item or not item.endswith((' County', ' Parish', ' Borough')):
            # Could be an MSA name - try to look it up
            try:
                # Use a simple heuristic: if it looks like a city/metro name (has hyphens, multiple words, etc.)
                # Only check items that are likely MSA names to avoid unnecessary queries
                looks_like_msa = (
                    '-' in item or 
                    len(item.split()) > 2 or 
                    any(word in item.lower() for word in ['metro', 'msa', 'cbsa', 'area', 'region'])
                )
                
                if looks_like_msa:
                    counties, _ = get_counties_by_msa_name(item)
                    if counties:
                        # It's an MSA name - expand it
                        expanded_list.extend(counties)
                        print(f"  Expanded MSA '{item}' to {len(counties)} counties")
                    else:
                        # Not an MSA name or couldn't find it - keep as is
                        expanded_list.append(item)
                else:
                    # Doesn't look like an MSA name - keep as is
                    expanded_list.append(item)
            except Exception as e:
                # If lookup fails, just keep the item as-is
                print(f"  Warning: Could not look up '{item}' as MSA: {e}")
                expanded_list.append(item)
        else:
            # Looks like a county name - keep as is
            expanded_list.append(item)
        
        processed += 1
        
        # Update progress every 10 items or at the end
        if progress_callback and (processed % 10 == 0 or processed == total_items):
            # Progress is between 8% and 10% for this step
            percent = 8 + int((processed / total_items) * 2)
            progress_callback({
                'percent': percent,
                'step': f'Checking for MSA names and expanding to counties... ({processed}/{total_items})',
                'done': False,
                'error': None
            })
    
    return expanded_list


def enrich_counties_with_metadata(
    counties: Union[List[str], List[Dict[str, str]]],
    geoids: List[str]
) -> List[Dict[str, str]]:
    """
    Enrich counties with full metadata from BigQuery (state_name, county_name, geoid5, cbsa_code, cbsa_name).
    
    Args:
        counties: Original county list (strings or dicts)
        geoids: List of GEOID5 codes that were mapped
    
    Returns:
        List of enriched county dictionaries with all metadata
    """
    if not geoids:
        return []

    try:
        client = get_bigquery_client(PROJECT_ID, app_name='MERGERMETER')

        # Query for all GEOID5s at once
        geoid5_str = ', '.join([f"'{g.zfill(5)}'" for g in geoids])
        
        query = f"""
        SELECT DISTINCT
            CAST(geoid5 AS STRING) as geoid5,
            County as county_name,
            State as state_name,
            CONCAT(County, ', ', State) as county_state,
            SUBSTR(LPAD(CAST(geoid5 AS STRING), 5, '0'), 1, 2) as state_code,
            SUBSTR(LPAD(CAST(geoid5 AS STRING), 5, '0'), 3, 3) as county_code,
            CAST(cbsa_code AS STRING) as cbsa_code,
            CBSA as cbsa_name
        FROM `{PROJECT_ID}.shared.cbsa_to_county`
        WHERE CAST(geoid5 AS STRING) IN ({geoid5_str})
        """
        
        results = execute_query(client, query)
        
        # Create a map from GEOID5 to metadata
        geoid_to_metadata = {}
        for row in results:
            geoid5 = str(row.get('geoid5', '')).zfill(5)
            if geoid5 and len(geoid5) == 5:
                geoid_to_metadata[geoid5] = {
                    'geoid5': geoid5,
                    'county_name': row.get('county_name', ''),
                    'state_name': row.get('state_name', ''),
                    'state_code': row.get('state_code', ''),
                    'county_code': row.get('county_code', ''),
                    'cbsa_code': row.get('cbsa_code', '') or '',
                    'cbsa_name': row.get('cbsa_name', '') or ''
                }
        
        # Build enriched counties list
        enriched_counties = []
        for geoid in geoids:
            geoid5 = str(geoid).zfill(5)
            if geoid5 in geoid_to_metadata:
                enriched_counties.append(geoid_to_metadata[geoid5])
            else:
                # Fallback: create minimal dict from GEOID5
                state_code = geoid5[:2] if len(geoid5) >= 2 else ''
                county_code = geoid5[2:] if len(geoid5) >= 5 else ''
                enriched_counties.append({
                    'geoid5': geoid5,
                    'county_name': '',
                    'state_name': '',
                    'state_code': state_code,
                    'county_code': county_code,
                    'cbsa_code': '',
                    'cbsa_name': ''
                })
        
        return enriched_counties
        
    except Exception as e:
        print(f"Error enriching counties with metadata: {e}")
        import traceback
        traceback.print_exc()
        # Return minimal enriched counties from GEOID5s
        enriched = []
        for geoid in geoids:
            geoid5 = str(geoid).zfill(5)
            state_code = geoid5[:2] if len(geoid5) >= 2 else ''
            county_code = geoid5[2:] if len(geoid5) >= 5 else ''
            enriched.append({
                'geoid5': geoid5,
                'county_name': '',
                'state_name': '',
                'state_code': state_code,
                'county_code': county_code,
                'cbsa_code': '',
                'cbsa_name': ''
            })
        return enriched