"""
BranchMapper Blueprint for main JustData app.
Converts the standalone BranchMapper app into a blueprint.
"""

from flask import Blueprint, render_template, request, jsonify, url_for, current_app, Response
from jinja2 import ChoiceLoader, FileSystemLoader
import os
import numpy as np
import re
from pathlib import Path

from justdata.main.auth import require_access, get_user_permissions, login_required
from justdata.shared.utils.unified_env import get_unified_config
from .config import TEMPLATES_DIR, STATIC_DIR
from .version import __version__

# FDIC BankFind Suite has required an API key on every request since 2026-09-08
FDIC_API_KEY = os.environ.get('FDIC_API_KEY', '')

# Import utilities from branchmapper's own modules
from .data_utils import (
    get_available_counties, get_available_states,
    execute_branch_query,
    get_county_fips, get_all_bank_names,
    execute_national_bank_query,
    execute_bounds_query,
    get_counties_in_bounds,
    get_states_overlapping_bounds,
    parse_fdic_events
)
from .core import load_sql_template
# Import from branchsight for shared functionality
from justdata.apps.branchsight.data_utils import expand_state_to_counties

# Get shared templates directory
REPO_ROOT = Path(__file__).parent.parent.parent.absolute()
SHARED_TEMPLATES_DIR = REPO_ROOT / 'shared' / 'web' / 'templates'

# Create blueprint
branchmapper_bp = Blueprint(
    'branchmapper',
    __name__,
    template_folder=TEMPLATES_DIR,
    static_folder=STATIC_DIR,
    static_url_path='/branchmapper/static'
)


@branchmapper_bp.record_once
def configure_template_loader(state):
    """Configure Jinja2 to search blueprint templates first.

    IMPORTANT: Blueprint templates must come FIRST in the ChoiceLoader so that
    app-specific templates are found before shared templates.

    NOTE: We do NOT add shared_loader here because the main app already includes
    shared templates. Adding it again would cause shared templates to be searched
    BEFORE other blueprint templates, leading to wrong template being rendered.
    """
    app = state.app
    blueprint_loader = FileSystemLoader(str(TEMPLATES_DIR))
    app.jinja_loader = ChoiceLoader([
        blueprint_loader,  # Blueprint templates first (highest priority)
        app.jinja_loader   # Main app loader (already includes shared templates)
    ])


@branchmapper_bp.route('/')
@login_required
@require_access('branchmapper', 'partial')
def index():
    """Main page with the interactive map"""
    user_permissions = get_user_permissions()
    app_base_url = url_for('branchmapper.index').rstrip('/')
    breadcrumb_items = [{'name': 'BranchMapper', 'url': '/branchmapper'}]
    
    # Get Mapbox configuration
    config = get_unified_config()
    mapbox_token = config.get('mapbox_token', os.environ.get('MAPBOX_ACCESS_TOKEN', ''))
    mapbox_style = config.get('mapbox_style', os.environ.get('MAPBOX_STYLE', 'mapbox://styles/jedlebi/cltg2vre600wz01p02c3jf3h3'))
    
    return render_template('branch_mapper_template.html',
                         version=__version__,
                         permissions=user_permissions,
                         app_base_url=app_base_url,
                         app_name='BranchMapper',
                         breadcrumb_items=breadcrumb_items,
                         mapbox_token=mapbox_token,
                         mapbox_style=mapbox_style)


@branchmapper_bp.route('/counties')
@login_required
@require_access('branchmapper', 'partial')
def counties():
    """Return a list of all available counties"""
    try:
        counties_list = get_available_counties()
        return jsonify(counties_list)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify([])


@branchmapper_bp.route('/states')
@login_required
@require_access('branchmapper', 'partial')
def states():
    """Return a list of all available states"""
    try:
        states_list = get_available_states()
        return jsonify(states_list)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify([])


@branchmapper_bp.route('/counties-by-state/<state_code>')
@login_required
@require_access('branchmapper', 'partial')
def counties_by_state(state_code):
    """Return a list of counties for a specific state"""
    try:
        counties_list = expand_state_to_counties(state_code)
        return jsonify(counties_list)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e), 'counties': []}), 500


@branchmapper_bp.route('/api/census-tracts/<county>')
@login_required
@require_access('branchmapper', 'partial')
def api_census_tracts(county):
    """Return census tract boundaries with income and/or minority data for a county"""
    try:
        # URL decode the county name (handles apostrophes and special characters)
        from urllib.parse import unquote
        county = unquote(county)

        from justdata.apps.branchmapper.census_tract_utils import (
            extract_fips_from_county_state,
            get_cbsa_for_county,
            get_county_median_family_income,
            get_county_minority_percentage,
            get_tract_income_data,
            get_tract_minority_data,
            get_tract_minority_data_by_cbsa,
            get_tract_boundaries_geojson,
            categorize_income_level,
            categorize_minority_level,
            get_census_api_key,
        )
        
        # Check which data types to include
        include_income = request.args.get('income', 'true').lower() == 'true'
        include_minority = request.args.get('minority', 'true').lower() == 'true'
        
        # Extract FIPS codes
        fips_data = extract_fips_from_county_state(county)
        if not fips_data:
            return jsonify({
                'success': False,
                'error': f'Could not find FIPS codes for {county}'
            }), 400
        
        state_fips = fips_data['state_fips']
        county_fips = fips_data['county_fips']
        
        print(f"Using county-level baselines for {county} (State FIPS: {state_fips}, County FIPS: {county_fips})")
        
        # Get tract boundaries first (needed for all data types)
        tract_boundaries = get_tract_boundaries_geojson(state_fips, county_fips)
        if not tract_boundaries:
            return jsonify({
                'success': False,
                'error': f'Could not fetch census tract boundaries'
            }), 500
        
        # Initialize lookup dictionaries
        income_lookup = {}
        minority_lookup = {}
        
        # Get income data if requested
        baseline_income = None
        if include_income:
            api_key = get_census_api_key()
            if not api_key:
                print(f"WARNING: CENSUS_API_KEY not set. Cannot fetch income data.")
                return jsonify({
                    'success': False,
                    'error': 'CENSUS_API_KEY environment variable is not set. Please configure it to use income layers.'
                }), 500
            
            baseline_income = get_county_median_family_income(state_fips, county_fips)
            if baseline_income:
                print(f"[OK] Using county median income: ${baseline_income:,.0f} for {county}")
            else:
                print(f"[ERROR] Failed to fetch county median income for {county}")
                return jsonify({
                    'success': False,
                    'error': f'Could not fetch county median income data from Census API. Please try again.'
                }), 500

            tract_income_data = get_tract_income_data(state_fips, county_fips)
            print(f"Fetched {len(tract_income_data)} tracts with income data")

            if not tract_income_data:
                print(f"[ERROR] No tract income data returned for {county}")
                return jsonify({
                    'success': False,
                    'error': f'Could not fetch tract income data from Census API. Please try again.'
                }), 500

            for tract in tract_income_data:
                geoid = tract['tract_geoid']
                geoid_normalized = str(geoid).zfill(11)
                income_lookup[geoid_normalized] = tract
                income_lookup[geoid] = tract
            print(f"Created income lookup with {len(income_lookup)} entries")
        
        # Get minority data if requested
        county_minority_pct = None
        minority_quartiles = None  # Will store Q1, Q2 (median), Q3 quartile values
        if include_minority:
            api_key = get_census_api_key()
            if not api_key:
                print(f"WARNING: CENSUS_API_KEY not set. Cannot fetch minority data.")
                return jsonify({
                    'success': False,
                    'error': 'CENSUS_API_KEY environment variable is not set. Please configure it to use minority layers.'
                }), 500
            
            county_minority_pct = get_county_minority_percentage(state_fips, county_fips)
            if county_minority_pct is not None:
                print(f"[OK] Using county minority percentage: {county_minority_pct:.1f}% for {county}")
            else:
                print(f"[ERROR] Failed to fetch county minority percentage for {county}")
                return jsonify({
                    'success': False,
                    'error': f'Could not fetch county minority data from Census API. Please try again.'
                }), 500

            tract_minority_data = get_tract_minority_data(state_fips, county_fips)
            print(f"Fetched {len(tract_minority_data)} tracts with minority data")

            if not tract_minority_data:
                print(f"[ERROR] No tract minority data returned for {county}")
                return jsonify({
                    'success': False,
                    'error': f'Could not fetch tract minority data from Census API. Please try again.'
                }), 500

            for tract in tract_minority_data:
                geoid = tract['tract_geoid']
                geoid_normalized = str(geoid).zfill(11)
                minority_lookup[geoid_normalized] = tract
                minority_lookup[geoid] = tract
            print(f"Created minority lookup with {len(minority_lookup)} entries")

            # Look up CBSA for this county to scope quartiles to metro area
            cbsa_info = get_cbsa_for_county(county)
            quartile_source_tracts = tract_minority_data  # default: single county
            quartile_scope = county
            cbsa_found = False

            if cbsa_info:
                cbsa_code = cbsa_info['cbsa_code']
                cbsa_name = cbsa_info.get('cbsa_name', cbsa_code)
                print(f"[OK] Found CBSA {cbsa_code} ({cbsa_name}) for {county}, using CBSA-scoped quartiles")
                cbsa_tracts = get_tract_minority_data_by_cbsa(cbsa_code, api_key)
                if cbsa_tracts:
                    quartile_source_tracts = cbsa_tracts
                    quartile_scope = cbsa_name
                    cbsa_found = True
                    print(f"[OK] Using {len(cbsa_tracts)} CBSA tracts for quartile calculation")
                else:
                    print(f"[WARNING] CBSA tract data empty, falling back to county-level quartiles")
            else:
                print(f"[INFO] No CBSA found for {county} (rural county), using county-level quartiles")

            # Calculate quartiles from CBSA-scoped (or fallback county) tracts
            minority_percentages = []
            for tract in quartile_source_tracts:
                minority_pct = tract.get('minority_percentage')
                if minority_pct is not None and minority_pct >= 0 and minority_pct <= 100:
                    minority_percentages.append(minority_pct)

            if minority_percentages:
                # Use numpy for accurate quartile calculation
                try:
                    q1 = np.percentile(minority_percentages, 25)
                    q2 = np.percentile(minority_percentages, 50)  # Median
                    q3 = np.percentile(minority_percentages, 75)
                    minority_quartiles = {'q1': float(q1), 'q2': float(q2), 'q3': float(q3)}
                    print(f"[OK] Calculated minority quartiles: Q1={q1:.1f}%, Q2={q2:.1f}%, Q3={q3:.1f}% (n={len(minority_percentages)} tracts, scope={quartile_scope})")
                except Exception as e:
                    # Fallback to manual calculation if numpy fails
                    print(f"[WARNING] Error using numpy for quartiles: {e}, using manual calculation")
                    minority_percentages.sort()
                    n = len(minority_percentages)
                    q1_idx = int(n * 0.25)
                    q2_idx = int(n * 0.50)
                    q3_idx = int(n * 0.75)
                    q1 = minority_percentages[q1_idx] if q1_idx < n else minority_percentages[-1]
                    q2 = minority_percentages[q2_idx] if q2_idx < n else minority_percentages[-1]
                    q3 = minority_percentages[q3_idx] if q3_idx < n else minority_percentages[-1]
                    minority_quartiles = {'q1': q1, 'q2': q2, 'q3': q3}
                    print(f"[OK] Calculated minority quartiles (manual): Q1={q1:.1f}%, Q2={q2:.1f}%, Q3={q3:.1f}% (n={n} tracts, scope={quartile_scope})")
            else:
                print(f"[WARNING] No valid minority percentages found to calculate quartiles")
        
        # Merge data with boundaries
        valid_features = []
        matched_count = 0
        filtered_count = 0
        
        for feature in tract_boundaries['features']:
            geoid = feature['properties'].get('GEOID')
            
            if geoid:
                geoid_str = str(geoid).strip()
                geoid_normalized = geoid_str.zfill(11)
            else:
                geoid_str = None
                geoid_normalized = None
            
            # Add income data
            if include_income:
                tract_data = income_lookup.get(geoid_normalized) or income_lookup.get(geoid_str) or income_lookup.get(geoid)
                
                if tract_data:
                    median_income = tract_data.get('median_family_income')
                    
                    if median_income is not None and median_income > 0:
                        income_category = categorize_income_level(median_income, baseline_income) if baseline_income else 'Unknown'
                        
                        feature['properties']['median_family_income'] = median_income
                        feature['properties']['income_category'] = income_category
                        feature['properties']['baseline_median_income'] = baseline_income
                        feature['properties']['baseline_type'] = 'county'
                        feature['properties']['income_ratio'] = (median_income / baseline_income) if median_income and baseline_income and baseline_income > 0 else None
                        matched_count += 1
                    else:
                        # Invalid income data - still include tract with Unknown category
                        if geoid:
                            print(f"Tract {geoid}: Invalid income data: {median_income}, marking as Unknown")
                        feature['properties']['median_family_income'] = None
                        feature['properties']['income_category'] = 'Unknown'
                        feature['properties']['baseline_median_income'] = baseline_income
                        feature['properties']['baseline_type'] = 'county'
                        feature['properties']['income_ratio'] = None
                else:
                    # No income data found - still include tract with Unknown category
                    if geoid:
                        print(f"Tract {geoid}: No income data found, marking as Unknown")
                    feature['properties']['median_family_income'] = None
                    feature['properties']['income_category'] = 'Unknown'
                    feature['properties']['baseline_median_income'] = baseline_income
                    feature['properties']['baseline_type'] = 'county'
                    feature['properties']['income_ratio'] = None
            
            # Add minority data
            if include_minority:
                tract_data = minority_lookup.get(geoid_normalized) or minority_lookup.get(geoid_str) or minority_lookup.get(geoid)
                
                if tract_data:
                    minority_pct = tract_data.get('minority_percentage')
                    total_pop = tract_data.get('total_population')
                    minority_pop = tract_data.get('minority_population')
                    
                    if minority_pct is not None and minority_pct >= 0 and minority_pct <= 100 and total_pop is not None and total_pop > 0:
                        # Categorize by quartile instead of ratio
                        if minority_quartiles:
                            q1_pct = round(minority_quartiles['q1'], 1)
                            q2_pct = round(minority_quartiles['q2'], 1)
                            q3_pct = round(minority_quartiles['q3'], 1)

                            if minority_pct < minority_quartiles['q1']:
                                minority_category = f'Lowest (<{q1_pct}%)'
                            elif minority_pct < minority_quartiles['q2']:
                                minority_category = f'Below median ({q1_pct}-{q2_pct}%)'
                            elif minority_pct < minority_quartiles['q3']:
                                minority_category = f'Above median ({q2_pct}-{q3_pct}%)'
                            else:
                                minority_category = f'Highest (>{q3_pct}%)'
                            minority_ratio = None  # Not using ratio for quartile-based categorization
                        else:
                            # Fallback to old method if quartiles not available
                            minority_category, minority_ratio = categorize_minority_level(minority_pct, county_minority_pct) if county_minority_pct else ('Unknown', None)

                        feature['properties']['minority_percentage'] = minority_pct
                        feature['properties']['minority_category'] = minority_category
                        feature['properties']['county_minority_percentage'] = county_minority_pct
                        feature['properties']['minority_ratio'] = minority_ratio
                        feature['properties']['total_population'] = total_pop
                        feature['properties']['minority_population'] = minority_pop
                    else:
                        # Invalid minority data - still include tract with Unknown category
                        if geoid:
                            print(f"Tract {geoid}: Invalid minority data (pct: {minority_pct}, pop: {total_pop}), marking as Unknown")
                        feature['properties']['minority_percentage'] = None
                        feature['properties']['minority_category'] = 'Unknown'
                        feature['properties']['county_minority_percentage'] = county_minority_pct
                        feature['properties']['minority_ratio'] = None
                        feature['properties']['total_population'] = None
                        feature['properties']['minority_population'] = None
                else:
                    # No minority data found - still include tract with Unknown category
                    if geoid:
                        print(f"Tract {geoid}: No minority data found, marking as Unknown")
                    feature['properties']['minority_percentage'] = None
                    feature['properties']['minority_category'] = 'Unknown'
                    feature['properties']['county_minority_percentage'] = county_minority_pct
                    feature['properties']['minority_ratio'] = None
                    feature['properties']['total_population'] = None
                    feature['properties']['minority_population'] = None
            
            if include_income or include_minority:
                valid_features.append(feature)
        
        # Update GeoJSON to only include features with valid data
        if include_income or include_minority:
            tract_boundaries['features'] = valid_features
            if include_income:
                print(f"Income layer: {matched_count} valid tracts, {filtered_count} invalid/water tracts filtered out")
            if include_minority:
                minority_matched = sum(1 for f in valid_features if f['properties'].get('minority_percentage') is not None and f['properties'].get('minority_percentage') >= 0)
                print(f"Minority layer: {minority_matched} valid tracts with minority data, {filtered_count} invalid/water tracts filtered out")
        
        result = {
            'success': True,
            'county': county,
            'tract_count': len(tract_boundaries['features']),
            'geojson': tract_boundaries
        }
        
        if include_income:
            result['baseline_median_family_income'] = baseline_income
            result['baseline_type'] = 'county'
        
        if include_minority:
            result['county_minority_percentage'] = county_minority_pct
            if minority_quartiles:
                result['minority_quartiles'] = minority_quartiles
                result['quartile_scope'] = quartile_scope
                result['quartile_tract_count'] = len(quartile_source_tracts)

        return jsonify(result)

    except Exception as e:
        error_msg = str(e)
        print(f"ERROR in api_census_tracts endpoint for county '{county}': {error_msg}")
        import traceback
        print("Full traceback:")
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': error_msg,
            'county': county,
            'message': f'Failed to load census tract data for {county}: {error_msg}'
        }), 500


@branchmapper_bp.route('/api/census-tracts-by-state/<state_fips>')
@login_required
@require_access('branchmapper', 'partial')
def api_census_tracts_by_state(state_fips):
    """Return census tract boundaries with income and/or minority data for an entire state"""
    try:
        from justdata.apps.branchmapper.census_tract_utils import (
            get_state_median_family_income,
            get_state_minority_percentage,
            get_tract_income_data_by_state,
            get_tract_minority_data_by_state,
            get_tract_boundaries_by_state,
            categorize_income_level,
            categorize_minority_level,
            get_census_api_key,
        )

        # Check which data types to include
        include_income = request.args.get('income', 'true').lower() == 'true'
        include_minority = request.args.get('minority', 'true').lower() == 'true'

        state_fips = str(state_fips).strip().zfill(2)

        print(f"Using state-level baselines for state FIPS: {state_fips}")

        # Get tract boundaries for the entire state
        tract_boundaries = get_tract_boundaries_by_state(state_fips)
        if not tract_boundaries:
            return jsonify({
                'success': False,
                'error': f'Could not fetch census tract boundaries for state {state_fips}'
            }), 500

        # Initialize lookup dictionaries
        income_lookup = {}
        minority_lookup = {}

        # Get income data if requested
        baseline_income = None
        if include_income:
            api_key = get_census_api_key()
            if not api_key:
                print(f"WARNING: CENSUS_API_KEY not set. Cannot fetch income data.")
                return jsonify({
                    'success': False,
                    'error': 'CENSUS_API_KEY environment variable is not set. Please configure it to use income layers.'
                }), 500

            baseline_income = get_state_median_family_income(state_fips)
            if baseline_income:
                print(f"[OK] Using state median income: ${baseline_income:,.0f} for state {state_fips}")
            else:
                print(f"[ERROR] Failed to fetch state median income for state {state_fips}")
                return jsonify({
                    'success': False,
                    'error': f'Could not fetch state median income data from Census API. Please try again.'
                }), 500

            tract_income_data = get_tract_income_data_by_state(state_fips)
            print(f"Fetched {len(tract_income_data)} tracts with income data")

            if not tract_income_data:
                print(f"[ERROR] No tract income data returned for state {state_fips}")
                return jsonify({
                    'success': False,
                    'error': f'Could not fetch tract income data from Census API. Please try again.'
                }), 500

            for tract in tract_income_data:
                geoid = tract['tract_geoid']
                geoid_normalized = str(geoid).zfill(11)
                income_lookup[geoid_normalized] = tract
                income_lookup[geoid] = tract
            print(f"Created income lookup with {len(income_lookup)} entries")

        # Get minority data if requested
        state_minority_pct = None
        minority_quartiles = None
        if include_minority:
            api_key = get_census_api_key()
            if not api_key:
                print(f"WARNING: CENSUS_API_KEY not set. Cannot fetch minority data.")
                return jsonify({
                    'success': False,
                    'error': 'CENSUS_API_KEY environment variable is not set. Please configure it to use minority layers.'
                }), 500

            state_minority_pct = get_state_minority_percentage(state_fips)
            if state_minority_pct is not None:
                print(f"[OK] Using state minority percentage: {state_minority_pct:.1f}% for state {state_fips}")
            else:
                print(f"[ERROR] Failed to fetch state minority percentage for state {state_fips}")
                return jsonify({
                    'success': False,
                    'error': f'Could not fetch state minority data from Census API. Please try again.'
                }), 500

            tract_minority_data = get_tract_minority_data_by_state(state_fips)
            print(f"Fetched {len(tract_minority_data)} tracts with minority data")

            if not tract_minority_data:
                print(f"[ERROR] No tract minority data returned for state {state_fips}")
                return jsonify({
                    'success': False,
                    'error': f'Could not fetch tract minority data from Census API. Please try again.'
                }), 500

            for tract in tract_minority_data:
                geoid = tract['tract_geoid']
                geoid_normalized = str(geoid).zfill(11)
                minority_lookup[geoid_normalized] = tract
                minority_lookup[geoid] = tract
            print(f"Created minority lookup with {len(minority_lookup)} entries")

            # Calculate quartiles for minority percentage
            minority_percentages = []
            for tract in tract_minority_data:
                minority_pct = tract.get('minority_percentage')
                if minority_pct is not None and minority_pct >= 0 and minority_pct <= 100:
                    minority_percentages.append(minority_pct)

            if minority_percentages:
                # Use numpy for accurate quartile calculation
                try:
                    q1 = np.percentile(minority_percentages, 25)
                    q2 = np.percentile(minority_percentages, 50)  # Median
                    q3 = np.percentile(minority_percentages, 75)
                    minority_quartiles = {'q1': float(q1), 'q2': float(q2), 'q3': float(q3)}
                    print(f"[OK] Calculated minority quartiles: Q1={q1:.1f}%, Q2={q2:.1f}%, Q3={q3:.1f}% (n={len(minority_percentages)} tracts)")
                except Exception as e:
                    # Fallback to manual calculation if numpy fails
                    print(f"[WARNING] Error using numpy for quartiles: {e}, using manual calculation")
                    minority_percentages.sort()
                    n = len(minority_percentages)
                    q1_idx = int(n * 0.25)
                    q2_idx = int(n * 0.50)
                    q3_idx = int(n * 0.75)
                    q1 = minority_percentages[q1_idx] if q1_idx < n else minority_percentages[-1]
                    q2 = minority_percentages[q2_idx] if q2_idx < n else minority_percentages[-1]
                    q3 = minority_percentages[q3_idx] if q3_idx < n else minority_percentages[-1]
                    minority_quartiles = {'q1': q1, 'q2': q2, 'q3': q3}
                    print(f"[OK] Calculated minority quartiles (manual): Q1={q1:.1f}%, Q2={q2:.1f}%, Q3={q3:.1f}% (n={n} tracts)")
            else:
                print(f"[WARNING] No valid minority percentages found to calculate quartiles")

        # Merge data with boundaries
        valid_features = []
        matched_count = 0
        filtered_count = 0

        for feature in tract_boundaries['features']:
            geoid = feature['properties'].get('GEOID')

            if geoid:
                geoid_str = str(geoid).strip()
                geoid_normalized = geoid_str.zfill(11)
            else:
                geoid_str = None
                geoid_normalized = None

            # Add income data
            if include_income:
                tract_data = income_lookup.get(geoid_normalized) or income_lookup.get(geoid_str) or income_lookup.get(geoid)

                if tract_data:
                    median_income = tract_data.get('median_family_income')

                    if median_income is not None and median_income > 0:
                        income_category = categorize_income_level(median_income, baseline_income) if baseline_income else 'Unknown'

                        feature['properties']['median_family_income'] = median_income
                        feature['properties']['income_category'] = income_category
                        feature['properties']['baseline_median_income'] = baseline_income
                        feature['properties']['baseline_type'] = 'state'
                        feature['properties']['income_ratio'] = (median_income / baseline_income) if median_income and baseline_income and baseline_income > 0 else None
                        matched_count += 1
                    else:
                        # Invalid income data - still include tract with Unknown category
                        if geoid:
                            print(f"Tract {geoid}: Invalid income data: {median_income}, marking as Unknown")
                        feature['properties']['median_family_income'] = None
                        feature['properties']['income_category'] = 'Unknown'
                        feature['properties']['baseline_median_income'] = baseline_income
                        feature['properties']['baseline_type'] = 'state'
                        feature['properties']['income_ratio'] = None
                else:
                    # No income data found - still include tract with Unknown category
                    if geoid:
                        print(f"Tract {geoid}: No income data found, marking as Unknown")
                    feature['properties']['median_family_income'] = None
                    feature['properties']['income_category'] = 'Unknown'
                    feature['properties']['baseline_median_income'] = baseline_income
                    feature['properties']['baseline_type'] = 'state'
                    feature['properties']['income_ratio'] = None

            # Add minority data
            if include_minority:
                tract_data = minority_lookup.get(geoid_normalized) or minority_lookup.get(geoid_str) or minority_lookup.get(geoid)

                if tract_data:
                    minority_pct = tract_data.get('minority_percentage')
                    total_pop = tract_data.get('total_population')
                    minority_pop = tract_data.get('minority_population')

                    if minority_pct is not None and minority_pct >= 0 and minority_pct <= 100 and total_pop is not None and total_pop > 0:
                        # Categorize by quartile instead of ratio
                        if minority_quartiles:
                            q1_pct = round(minority_quartiles['q1'], 1)
                            q2_pct = round(minority_quartiles['q2'], 1)
                            q3_pct = round(minority_quartiles['q3'], 1)

                            if minority_pct < minority_quartiles['q1']:
                                minority_category = f'Lowest (<{q1_pct}%)'
                            elif minority_pct < minority_quartiles['q2']:
                                minority_category = f'Below median ({q1_pct}-{q2_pct}%)'
                            elif minority_pct < minority_quartiles['q3']:
                                minority_category = f'Above median ({q2_pct}-{q3_pct}%)'
                            else:
                                minority_category = f'Highest (>{q3_pct}%)'
                            minority_ratio = None  # Not using ratio for quartile-based categorization
                        else:
                            # Fallback to old method if quartiles not available
                            minority_category, minority_ratio = categorize_minority_level(minority_pct, state_minority_pct) if state_minority_pct else ('Unknown', None)

                        feature['properties']['minority_percentage'] = minority_pct
                        feature['properties']['minority_category'] = minority_category
                        feature['properties']['state_minority_percentage'] = state_minority_pct
                        feature['properties']['minority_ratio'] = minority_ratio
                        feature['properties']['total_population'] = total_pop
                        feature['properties']['minority_population'] = minority_pop
                    else:
                        # Invalid minority data - still include tract with Unknown category
                        if geoid:
                            print(f"Tract {geoid}: Invalid minority data (pct: {minority_pct}, pop: {total_pop}), marking as Unknown")
                        feature['properties']['minority_percentage'] = None
                        feature['properties']['minority_category'] = 'Unknown'
                        feature['properties']['state_minority_percentage'] = state_minority_pct
                        feature['properties']['minority_ratio'] = None
                        feature['properties']['total_population'] = None
                        feature['properties']['minority_population'] = None
                else:
                    # No minority data found - still include tract with Unknown category
                    if geoid:
                        print(f"Tract {geoid}: No minority data found, marking as Unknown")
                    feature['properties']['minority_percentage'] = None
                    feature['properties']['minority_category'] = 'Unknown'
                    feature['properties']['state_minority_percentage'] = state_minority_pct
                    feature['properties']['minority_ratio'] = None
                    feature['properties']['total_population'] = None
                    feature['properties']['minority_population'] = None

            if include_income or include_minority:
                valid_features.append(feature)

        # Update GeoJSON to only include features with valid data
        if include_income or include_minority:
            tract_boundaries['features'] = valid_features
            if include_income:
                print(f"Income layer: {matched_count} valid tracts, {filtered_count} invalid/water tracts filtered out")
            if include_minority:
                minority_matched = sum(1 for f in valid_features if f['properties'].get('minority_percentage') is not None and f['properties'].get('minority_percentage') >= 0)
                print(f"Minority layer: {minority_matched} valid tracts with minority data, {filtered_count} invalid/water tracts filtered out")

        result = {
            'success': True,
            'state_fips': state_fips,
            'tract_count': len(tract_boundaries['features']),
            'geojson': tract_boundaries
        }

        if include_income:
            result['baseline_median_family_income'] = baseline_income
            result['baseline_type'] = 'state'

        if include_minority:
            result['state_minority_percentage'] = state_minority_pct
            if minority_quartiles:
                result['minority_quartiles'] = minority_quartiles
                result['quartile_scope'] = f'State {state_fips}'
                result['quartile_tract_count'] = len(tract_minority_data)

        return jsonify(result)

    except Exception as e:
        error_msg = str(e)
        print(f"ERROR in api_census_tracts_by_state endpoint for state '{state_fips}': {error_msg}")
        import traceback
        print("Full traceback:")
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': error_msg,
            'state_fips': state_fips,
            'message': f'Failed to load census tract data for state {state_fips}: {error_msg}'
        }), 500

@branchmapper_bp.route('/api/branches')
@login_required
@require_access('branchmapper', 'partial')
def api_branches():
    """Return branch data with coordinates for map display"""
    try:
        county = request.args.get('county', '').strip()
        year_str = request.args.get('year', '').strip()
        
        if not county or not year_str:
            return jsonify({'error': 'County and year parameters are required'}), 400
        
        try:
            year = int(year_str)
        except ValueError:
            return jsonify({'error': 'Year must be a valid integer'}), 400
        
        # Load SQL template and execute query
        sql_template = load_sql_template()
        branches = execute_branch_query(sql_template, county, year)
        
        # Convert to JSON-serializable format
        result = []
        for branch in branches:
            branch_dict = {}
            for key, value in branch.items():
                # Handle numpy types and NaN values
                if hasattr(value, 'item'):
                    branch_dict[key] = value.item() if not np.isnan(value) else None
                elif isinstance(value, (np.integer, np.floating)):
                    branch_dict[key] = float(value) if not np.isnan(value) else None
                elif value is None or (isinstance(value, float) and np.isnan(value)):
                    branch_dict[key] = None
                else:
                    branch_dict[key] = value
            
            # Clean bank name
            if 'bank_name' in branch_dict and branch_dict['bank_name']:
                bank_name = str(branch_dict['bank_name']).strip()
                bank_name = re.sub(r'^THE\s+', '', bank_name, flags=re.IGNORECASE).strip()
                
                patterns = [
                    r',?\s*NATIONAL\s+ASSOCIATION\s*$',
                    r',?\s*N\.?\s*A\.?\s*$',
                    r',?\s*NA\s*$',
                    r',?\s*FEDERAL\s+SAVINGS\s+BANK\s*$',
                    r',?\s*FSB\s*$',
                ]
                
                for pattern in patterns:
                    bank_name = re.sub(pattern, '', bank_name, flags=re.IGNORECASE).strip()
                
                bank_name = re.sub(r'[,.\s]+$', '', bank_name).strip()
                branch_dict['bank_name'] = bank_name
            
            # Map service_type to branch_type
            if 'service_type' in branch_dict and branch_dict['service_type']:
                service_type = str(branch_dict['service_type']).strip()
                service_type_map = {
                    '11': 'Full Service, brick and mortar office',
                    '12': 'Full Service, retail office',
                    '13': 'Full Service, cyber office',
                }
                branch_dict['branch_type'] = service_type_map.get(service_type, f'Service Type {service_type}')
            
            result.append(branch_dict)
        
        return jsonify({
            'success': True,
            'branches': result,
            'count': len(result)
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@branchmapper_bp.route('/api/oscr-events')
@login_required
@require_access('branchmapper', 'partial')
def api_oscr_events():
    """Return FDIC OSCR branch events for a county and date range."""
    import requests as http_requests
    from datetime import datetime

    county = request.args.get('county', '').strip()
    start_date = request.args.get('start_date', '2023-02-01').strip()
    end_date = request.args.get('end_date', '').strip()

    if not end_date:
        end_date = datetime.now().strftime('%Y-%m-%d')

    if not county:
        return jsonify({'error': 'County parameter is required'}), 400

    fips = get_county_fips(county)
    if not fips:
        return jsonify({'error': f'Could not find FIPS codes for {county}'}), 400

    state_fips = fips['state_fips']
    county_fips = fips['county_fips']

    filters = (
        f"CHANGECODE:[711 TO 722] "
        f"AND OFF_PSTNUM:{state_fips} "
        f"AND OFF_CNTYNUM:{county_fips} "
        f"AND PROCDATE:[{start_date} TO {end_date}]"
    )

    fields = (
        "INSTNAME,CERT,OFF_NAME,OFF_PADDR,OFF_PCITY,OFF_PSTALP,"
        "OFF_PZIP5,OFF_CNTYNAME,OFF_LATITUDE,OFF_LONGITUDE,CHANGECODE,"
        "EFFDATE,PROCDATE,BKCLASS,OFF_SERVTYPE_DESC,"
        "FRM_OFF_LATITUDE,FRM_OFF_LONGITUDE,FRM_OFF_PADDR,FRM_OFF_PCITY,"
        "FRM_OFF_PSTALP,FRM_OFF_PZIP5,FRM_OFF_NAME"
    )

    fdic_url = "https://api.fdic.gov/banks/history"
    params = {
        'filters': filters,
        'fields': fields,
        'sort_by': 'PROCDATE',
        'sort_order': 'DESC',
        'limit': 10000
    }
    if FDIC_API_KEY:
        params['api_key'] = FDIC_API_KEY

    try:
        resp = http_requests.get(fdic_url, params=params, timeout=30)
        resp.raise_for_status()
        fdic_data = resp.json()
    except Exception as e:
        return jsonify({'error': f'FDIC API error: {str(e)}'}), 500

    result = parse_fdic_events(fdic_data)

    return jsonify({
        'success': True,
        'events': result['events'],
        'count': result['count'],
        'openings': result['openings'],
        'closings': result['closings'],
        'relocation_pairs': len(result['relocation_pairs']),
        'date_range': {'start': start_date, 'end': end_date}
    })


@branchmapper_bp.route('/api/bank-list')
@login_required
@require_access('branchmapper', 'partial')
def api_bank_list():
    """Return list of all bank names for search dropdown."""
    try:
        banks = get_all_bank_names()
        return jsonify({'success': True, 'banks': banks, 'count': len(banks)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@branchmapper_bp.route('/api/branches-by-bank')
@login_required
@require_access('branchmapper', 'partial')
def api_branches_by_bank():
    """Return all branches for a specific bank nationwide."""
    try:
        bank_name = request.args.get('bank_name', '').strip()
        year = int(request.args.get('year', '2025'))

        if not bank_name:
            return jsonify({'error': 'bank_name parameter is required'}), 400

        branches = execute_national_bank_query(bank_name, year)

        # Convert to JSON-serializable format with clean names
        result = []
        for branch in branches:
            branch_dict = {}
            for key, value in branch.items():
                if hasattr(value, 'item'):
                    branch_dict[key] = value.item() if not (hasattr(value, '__float__') and np.isnan(float(value))) else None
                elif isinstance(value, (np.integer, np.floating)):
                    branch_dict[key] = float(value) if not np.isnan(value) else None
                elif value is None or (isinstance(value, float) and np.isnan(value)):
                    branch_dict[key] = None
                else:
                    branch_dict[key] = value

            # Clean bank name
            if 'bank_name' in branch_dict and branch_dict['bank_name']:
                name = str(branch_dict['bank_name']).strip()
                name = re.sub(r'^THE\s+', '', name, flags=re.IGNORECASE).strip()
                for pattern in [r',?\s*NATIONAL\s+ASSOCIATION\s*$', r',?\s*N\.?\s*A\.?\s*$', r',?\s*NA\s*$', r',?\s*FEDERAL\s+SAVINGS\s+BANK\s*$', r',?\s*FSB\s*$']:
                    name = re.sub(pattern, '', name, flags=re.IGNORECASE).strip()
                name = re.sub(r'[,.\s]+$', '', name).strip()
                branch_dict['bank_name'] = name

            # Map service_type
            if 'service_type' in branch_dict and branch_dict['service_type']:
                st = str(branch_dict['service_type']).strip()
                st_map = {'11': 'Full Service, brick and mortar office', '12': 'Full Service, retail office', '13': 'Full Service, cyber office'}
                branch_dict['branch_type'] = st_map.get(st, f'Service Type {st}')

            result.append(branch_dict)

        return jsonify({'success': True, 'branches': result, 'count': len(result)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@branchmapper_bp.route('/api/branches-in-bounds')
@login_required
@require_access('branchmapper', 'partial')
def api_branches_in_bounds():
    """Return all branches within a geographic bounding box."""
    try:
        sw_lat = float(request.args.get('sw_lat', 0))
        sw_lng = float(request.args.get('sw_lng', 0))
        ne_lat = float(request.args.get('ne_lat', 0))
        ne_lng = float(request.args.get('ne_lng', 0))
        year = int(request.args.get('year', '2025'))

        if sw_lat == 0 and ne_lat == 0:
            return jsonify({'error': 'Bounding box parameters required'}), 400

        branches = execute_bounds_query(sw_lat, sw_lng, ne_lat, ne_lng, year)

        # Convert to JSON-serializable format with clean names
        result = []
        for branch in branches:
            branch_dict = {}
            for key, value in branch.items():
                if hasattr(value, 'item'):
                    branch_dict[key] = value.item() if not (hasattr(value, '__float__') and np.isnan(float(value))) else None
                elif isinstance(value, (np.integer, np.floating)):
                    branch_dict[key] = float(value) if not np.isnan(value) else None
                elif value is None or (isinstance(value, float) and np.isnan(value)):
                    branch_dict[key] = None
                else:
                    branch_dict[key] = value

            # Clean bank name
            if 'bank_name' in branch_dict and branch_dict['bank_name']:
                name = str(branch_dict['bank_name']).strip()
                name = re.sub(r'^THE\s+', '', name, flags=re.IGNORECASE).strip()
                for pattern in [r',?\s*NATIONAL\s+ASSOCIATION\s*$', r',?\s*N\.?\s*A\.?\s*$', r',?\s*NA\s*$', r',?\s*FEDERAL\s+SAVINGS\s+BANK\s*$', r',?\s*FSB\s*$']:
                    name = re.sub(pattern, '', name, flags=re.IGNORECASE).strip()
                name = re.sub(r'[,.\s]+$', '', name).strip()
                branch_dict['bank_name'] = name

            # Map service_type
            if 'service_type' in branch_dict and branch_dict['service_type']:
                st = str(branch_dict['service_type']).strip()
                st_map = {'11': 'Full Service, brick and mortar office', '12': 'Full Service, retail office', '13': 'Full Service, cyber office'}
                branch_dict['branch_type'] = st_map.get(st, f'Service Type {st}')

            result.append(branch_dict)

        return jsonify({'success': True, 'branches': result, 'count': len(result)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 500


@branchmapper_bp.route('/api/counties-in-bounds')
@login_required
@require_access('branchmapper', 'partial')
def api_counties_in_bounds():
    """Return counties with branches in a bounding box."""
    try:
        sw_lat = float(request.args.get('sw_lat', 0))
        sw_lng = float(request.args.get('sw_lng', 0))
        ne_lat = float(request.args.get('ne_lat', 0))
        ne_lng = float(request.args.get('ne_lng', 0))

        if sw_lat == 0 and ne_lat == 0:
            return jsonify({'error': 'Bounding box parameters required'}), 400

        counties = get_counties_in_bounds(sw_lat, sw_lng, ne_lat, ne_lng)
        return jsonify({'success': True, 'counties': counties, 'count': len(counties)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@branchmapper_bp.route('/api/oscr-events-by-bank')
@login_required
@require_access('branchmapper', 'partial')
def api_oscr_events_by_bank():
    """Return FDIC OSCR events for a specific bank (nationwide)."""
    import requests as http_requests
    from datetime import datetime

    bank_name = request.args.get('bank_name', '').strip()
    start_date = request.args.get('start_date', '2023-02-01').strip()
    end_date = request.args.get('end_date', '').strip()
    if not end_date:
        end_date = datetime.now().strftime('%Y-%m-%d')

    if not bank_name:
        return jsonify({'error': 'bank_name parameter is required'}), 400

    filters = (
        f'CHANGECODE:[711 TO 722] '
        f'AND INSTNAME:"{bank_name}" '
        f'AND PROCDATE:[{start_date} TO {end_date}]'
    )

    fields = (
        "INSTNAME,CERT,OFF_NAME,OFF_PADDR,OFF_PCITY,OFF_PSTALP,"
        "OFF_PZIP5,OFF_CNTYNAME,OFF_LATITUDE,OFF_LONGITUDE,CHANGECODE,"
        "EFFDATE,PROCDATE,BKCLASS,OFF_SERVTYPE_DESC,"
        "FRM_OFF_LATITUDE,FRM_OFF_LONGITUDE,FRM_OFF_PADDR,FRM_OFF_PCITY,"
        "FRM_OFF_PSTALP,FRM_OFF_PZIP5,FRM_OFF_NAME"
    )

    fdic_url = "https://api.fdic.gov/banks/history"
    params = {
        'filters': filters,
        'fields': fields,
        'sort_by': 'PROCDATE',
        'sort_order': 'DESC',
        'limit': 10000
    }
    if FDIC_API_KEY:
        params['api_key'] = FDIC_API_KEY

    try:
        resp = http_requests.get(fdic_url, params=params, timeout=30)
        resp.raise_for_status()
        fdic_data = resp.json()
    except Exception as e:
        return jsonify({'error': f'FDIC API error: {str(e)}'}), 500

    result = parse_fdic_events(fdic_data)

    return jsonify({
        'success': True,
        'events': result['events'],
        'count': result['count'],
        'openings': result['openings'],
        'closings': result['closings'],
        'relocation_pairs': len(result['relocation_pairs']),
        'date_range': {'start': start_date, 'end': end_date}
    })


@branchmapper_bp.route('/api/oscr-events-in-bounds')
@login_required
@require_access('branchmapper', 'partial')
def api_oscr_events_in_bounds():
    """Return FDIC OSCR events within a geographic bounding box."""
    import requests as http_requests
    from datetime import datetime

    sw_lat = float(request.args.get('sw_lat', 0))
    sw_lng = float(request.args.get('sw_lng', 0))
    ne_lat = float(request.args.get('ne_lat', 0))
    ne_lng = float(request.args.get('ne_lng', 0))
    start_date = request.args.get('start_date', '2023-02-01').strip()
    end_date = request.args.get('end_date', '').strip()
    if not end_date:
        end_date = datetime.now().strftime('%Y-%m-%d')

    if sw_lat == 0 and ne_lat == 0:
        return jsonify({'error': 'Bounding box parameters required'}), 400

    # Get state FIPS codes overlapping the bounds
    state_fips_list = get_states_overlapping_bounds(sw_lat, sw_lng, ne_lat, ne_lng)
    if not state_fips_list:
        return jsonify({'success': True, 'events': [], 'count': 0, 'openings': 0, 'closings': 0, 'relocation_pairs': 0})

    # Cap at 5 states to avoid excessive API calls
    state_fips_list = state_fips_list[:5]

    fields = (
        "INSTNAME,CERT,OFF_NAME,OFF_PADDR,OFF_PCITY,OFF_PSTALP,"
        "OFF_PZIP5,OFF_CNTYNAME,OFF_LATITUDE,OFF_LONGITUDE,CHANGECODE,"
        "EFFDATE,PROCDATE,BKCLASS,OFF_SERVTYPE_DESC,"
        "FRM_OFF_LATITUDE,FRM_OFF_LONGITUDE,FRM_OFF_PADDR,FRM_OFF_PCITY,"
        "FRM_OFF_PSTALP,FRM_OFF_PZIP5,FRM_OFF_NAME"
    )

    fdic_url = "https://api.fdic.gov/banks/history"
    all_fdic_data = {'data': []}

    for state_fips in state_fips_list:
        filters = (
            f"CHANGECODE:[711 TO 722] "
            f"AND OFF_PSTNUM:{state_fips} "
            f"AND PROCDATE:[{start_date} TO {end_date}]"
        )
        params = {
            'filters': filters,
            'fields': fields,
            'sort_by': 'PROCDATE',
            'sort_order': 'DESC',
            'limit': 10000
        }
        if FDIC_API_KEY:
            params['api_key'] = FDIC_API_KEY
        try:
            resp = http_requests.get(fdic_url, params=params, timeout=30)
            resp.raise_for_status()
            state_data = resp.json()
            all_fdic_data['data'].extend(state_data.get('data', []))
        except Exception as e:
            print(f"FDIC API error for state {state_fips}: {e}")
            continue

    result = parse_fdic_events(all_fdic_data)

    # Post-filter events to bounding box
    filtered_events = []
    for event in result['events']:
        lat = event.get('latitude')
        lng = event.get('longitude')
        if lat is not None and lng is not None:
            if sw_lat <= lat <= ne_lat and sw_lng <= lng <= ne_lng:
                filtered_events.append(event)
        else:
            # Include events without coords (they'll be geocoded on frontend)
            filtered_events.append(event)

    return jsonify({
        'success': True,
        'events': filtered_events,
        'count': len(filtered_events),
        'openings': len([e for e in filtered_events if e.get('display_type') == 'opening']),
        'closings': len([e for e in filtered_events if e.get('display_type') == 'closing']),
        'relocation_pairs': len(result['relocation_pairs']),
        'date_range': {'start': start_date, 'end': end_date}
    })


@branchmapper_bp.route('/export/methods-pdf')
@login_required
@require_access('branchmapper', 'partial')
def export_methods_pdf():
    """Generate and return the BranchMapper Methods & Definitions PDF."""
    geography = request.args.get('geography', 'Selected area')

    from justdata.apps.branchmapper.pdf_methods import generate_methods_pdf
    pdf_buffer = generate_methods_pdf(geography)

    return Response(
        pdf_buffer.getvalue(),
        mimetype='application/pdf',
        headers={
            'Content-Disposition': 'attachment; filename=BranchMapper_Methods_and_Definitions.pdf'
        }
    )


@branchmapper_bp.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'app': 'branchmapper',
        'version': __version__
    })

