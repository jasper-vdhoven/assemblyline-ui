
from flask import request

from assemblyline.datastore.collection import Index
from assemblyline.odm.models.user import ROLES
from assemblyline_core.submission_client import SubmissionException
from assemblyline_ui.api.base import api_login, make_api_response, make_subapi_blueprint
from assemblyline_ui.config import STORAGE, config, CLASSIFICATION as Classification, ARCHIVE_MANAGER

SUB_API = 'archive'

archive_api = make_subapi_blueprint(SUB_API, api_version=4)
archive_api._doc = "Perform operations on archived submissions"


@archive_api.route("/<sid>/", methods=["PUT"])
@api_login(require_role=[ROLES.archive_trigger])
def archive_submission(sid, **kwargs):
    """
    Send a submission to the permanent archive

    Variables:
    sid         => ID of the submission to send to the archive

    Arguments:
    delete_after     => Delete data from hot storage after the move ? (Default: False)

    Data Block:
    None

    API call example:
    /api/v4/archive/12345...67890/

    Result example:
    {
     "success": True,      # Was the archiving operation successful
     "action": "archive",  # Which operation took place (archive or resubmit)
     "sid": None           # (Optional) Submission ID of the new submission with extended
                           #            service selection
    }
    """
    if not config.datastore.archive.enabled:
        return make_api_response({"success": False}, "Archiving is disabled on the server.", 403)

    user = kwargs['user']
    delete_after = request.args.get('delete_after', 'false').lower() in ['true', '']
    submission = STORAGE.submission.get_if_exists(sid, as_obj=False)
    if not submission:
        return make_api_response({"success": False}, f"The submission '{sid}' was not found in the system", 404)

    if not user or not Classification.is_accessible(user['classification'], submission['classification']):
        return make_api_response({"success": False}, f"The submission '{sid}' is not accessible by this user", 403)

    try:
        archive_action = ARCHIVE_MANAGER.archive_submission(submission=submission, delete_after=delete_after)
        archive_action['success'] = True
        return make_api_response(archive_action)

    except SubmissionException as se:
        return make_api_response({"success": False}, err=str(se), status_code=400)


@archive_api.route("/details/<sha256>/", methods=["GET", "POST"])
@api_login(require_role=[ROLES.submission_view])
def get_additional_details(sha256, **kwargs):
    """
    Get additional details in the archive file details

    Variables:
    sha256                  => A resource locator for the file (SHA256)

    Arguments:
    offset                  => Offset at which we start giving files
    rows                    => Numbers of files to return
    sort                    => How to sort the results (not available in deep paging)

    Data Block:
    {
        "offset": 0,        # Offset in the results
        "rows": 100,        # Max number of results
        "sort": "field asc",# How to sort the results
    }

    API call example:
    /api/v4/file/result/123456...654321/

    Result example:
    {
        "tlsh": {           # List of files related by their tlsh
            "items": []     # List of files hash
            "count": 100,   # Number of files returned
            "offset": 0,    # Offset in the file list
            "total": 201,   # Total files found
        },
        "ssdeep1": {...},   # List of files related by the first part of their ssdeep
        "ssdeep2": {...},   # List of files related by the second part of their ssdeep
        "vector": {...}     # List of files related by their vector
    }
    """
    user = kwargs['user']
    file_obj = STORAGE.file.get(sha256, as_obj=False)

    if not file_obj:
        return make_api_response({}, "This file does not exists", 404)

    if not user or not Classification.is_accessible(user['classification'], file_obj['classification']):
        return make_api_response({}, "You are not allowed to view this file", 403)

    # Set the default search parameters
    params = {}
    params.setdefault('offset', 0)
    params.setdefault('rows', 10)
    params.setdefault('sort', 'seen.last desc')
    params.setdefault('fl', 'type,sha256,seen.last')
    params.setdefault('filters', [f'NOT(sha256:"{sha256}")'])
    params.setdefault('access_control', user['access_control'])
    params.setdefault('as_obj', False)
    params.setdefault('index_type', Index.HOT_AND_ARCHIVE)

    fields = ["offset", "rows", "sort"]

    req_data = None
    if request.method == "POST":
        req_data = request.json
    else:
        req_data = request.args

    params.update({k: req_data.get(k, None) for k in fields if req_data.get(k, None) is not None})

    output = {'tlsh': {}, 'ssdeep1': {}, 'ssdeep2': {}, 'vector': {}}

    # Process tlsh
    try:
        tlsh = file_obj['tlsh'].replace('/', '\\/')
        output['tlsh'] = STORAGE.file.search(query=f"tlsh:{tlsh}", **params)
    except Exception as e:
        output['tlsh'] = f"SearchException: {e}"

    # Process ssdeep
    try:
        ssdeep = file_obj.get('ssdeep', '').replace('/', '\\/').split(':')
        output['ssdeep1'] = STORAGE.file.search(query=f"ssdeep:{ssdeep[1]}~", **params)
        output['ssdeep2'] = STORAGE.file.search(query=f"ssdeep:{ssdeep[2]}~", **params)
    except Exception as e:
        output['ssdeep1'] = f"SearchException: {e}"
        output['ssdeep2'] = f"SearchException: {e}"



    # Process vector
    try:
        results = STORAGE.result.search(
            f"sha256:{sha256} AND response.service_name:APIVector", sort="created desc", as_obj=False)
        results = STORAGE.result.multiget(
            [result['id'] for result in results.get('items', None)],
            as_dictionary=False, as_obj=False)

        vector = []
        for result in results:
            for section in result['result']['sections']:
                vector.extend(section['tags']['vector'])

        query = ' OR '.join([f"result.sections.tags.vector:{v}" for v in vector])
        results = STORAGE.result.search(query=query, as_obj=False)
        ids = set([x['id'].split('.')[0] for x in STORAGE.result.stream_search(query=query, fl="id", as_obj=False)])
        query = ' OR '.join(f"sha256:{id}" for id in ids)
        output['vector'] = STORAGE.file.search(query=query, **params)
    except Exception as e:
        output['vector'] = f"SearchException: {e}"

    return make_api_response(output)
