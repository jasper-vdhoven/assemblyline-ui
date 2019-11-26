import concurrent.futures

from flask import request
from hashlib import sha256

from assemblyline.common import forge
from assemblyline.common.isotime import iso_to_epoch, now_as_iso
from assemblyline.common.memory_zip import InMemoryZip
from assemblyline.common.uid import get_id_from_data, SHORT
from assemblyline.odm.models.signature import DEPLOYED_STATUSES, STALE_STATUSES, DRAFT_STATUSES
from assemblyline.remote.datatypes.lock import Lock
from assemblyline_ui.api.base import api_login, make_api_response, make_file_response, make_subapi_blueprint
from assemblyline_ui.config import LOGGER, STORAGE
Classification = forge.get_classification()
config = forge.get_config()

SUB_API = 'signature'
signature_api = make_subapi_blueprint(SUB_API, api_version=4)
signature_api._doc = "Perform operations on signatures"

DEFAULT_CACHE_TTL = 24 * 60 * 60  # 1 Day


@signature_api.route("/add/", methods=["PUT"])
@api_login(audit=False, required_priv=['W'], allow_readonly=False, require_type=['signature_importer'])
def add_signature(**_):
    """
    Add a signature to the system and assigns it a new ID
        WARNING: If two person call this method at exactly the
                 same time, they might get the same ID.
       
    Variables:
    None
    
    Arguments: 
    None
    
    Data Block (REQUIRED): # Signature block
    {"name": "sig_name",           # Signature name
     "type": "yara",               # One of yara, suricata or tagcheck
     "data": "rule sample {...}",  # Data of the rule to be added
     "source": "yara_signatures"   # Source from where the signature has been gathered
    }

    Result example:
    {"success": true,            #If saving the rule was a success or not
     "id": "<TYPE>_<SID>_<REVISION>"}  #ID that was assigned to the signature
    """
    data = request.json

    if data.get('type', None) is None or data['name'] is None or data['data'] is None:
        return make_api_response("", f"Signature name, type and data are mandatory fields.", 400)

    # Compute signature ID if missing
    data['signature_id'] = data.get('signature_id', get_id_from_data(data['data'], SHORT))
    key = f"{data['type']}_{data['signature_id']}_{data['revision']}"

    # Test signature name
    check_name_query = f"name:{data['name']} " \
                       f"AND type:{data['type']} " \
                       f"AND source:{data['source']} " \
                       f"AND NOT id:{data['signature_id']}*"
    other = STORAGE.signature.search(check_name_query, fl='id', rows='0')
    if other['total'] > 0:
        return make_api_response(
            {"success": False},
            "A signature with that name already exists",
            400
        )

    # Save the signature
    return make_api_response({"success": STORAGE.signature.save(key, data),
                              "id": key})


@signature_api.route("/sources/<service>/", methods=["PUT"])
@api_login(audit=False, required_priv=['W'], allow_readonly=False, require_type=['admin', 'signature_manager'])
def add_signature_source(service, **_):
    """
    Add a signature source for a given service

    Variables:
    service           =>      Service to which we want to add the source to

    Arguments:
    None

    Data Block:
    {
      "uri": "http://somesite/file_to_get",   # URI to fetch for parsing the rules
      "name": "signature_file.yar",           # Name of the file we will parse the rules as
      "username": null,                       # Username used to get to the URI
      "password": null,                       # Password used to get to the URI
      "header": {                             # Header sent during the request to the URI
        "X_TOKEN": "SOME RANDOM TOKEN"          # Exemple of header
      },
      "public_key": null,                     # Public key used to get to the URI
      "pattern": "^*.yar$"                    # Regex pattern use to get appropriate files from the URI
    }

    Result example:
    {"success": True/False}   # if the operation succeeded of not
    """
    try:
        data = request.json
    except (ValueError, KeyError):
        return make_api_response({"success": False},
                                 err="Invalid source object data",
                                 status_code=400)

    service_data = STORAGE.get_service_with_delta(service, as_obj=False)
    if not service_data.get('update_config', {}).get('generates_signatures', False):
        return make_api_response({"success": False},
                                 err="This service does not generate alerts therefor "
                                     "you cannot add a source to get the alerts from",
                                 status_code=400)

    current_sources = service_data.get('update_config', {}).get('sources', [])
    for source in current_sources:
        if source['name'] == data['name']:
            return make_api_response({"success": False},
                                     err=f"Update source filename already exist: {data['name']}",
                                     status_code=400)

        if source['uri'] == data['uri']:
            return make_api_response({"success": False},
                                     err=f"Update source uri already exist: {data['uri']}",
                                     status_code=400)

    current_sources.append(data)
    service_delta = STORAGE.service_delta.get(service, as_obj=False)
    if service_delta.get('update_config') is None:
        service_delta['update_config'] = {"sources": current_sources}
    else:
        service_delta['update_config']['sources'] = current_sources

    # Save the signature
    return make_api_response({"success": STORAGE.service_delta.save(service, service_delta)})


# noinspection PyPep8Naming
@signature_api.route("/change_status/<sid>/<status>/", methods=["GET"])
@api_login(required_priv=['W'], allow_readonly=False, require_type=['admin', 'signature_manager'])
def change_status(sid, status, **kwargs):
    """
    Change the status of a signature
       
    Variables:
    sid    =>  ID of the signature
    status  =>  New state
    
    Arguments: 
    None
    
    Data Block:
    None
    
    Result example:
    { "success" : true }      #If saving the rule was a success or not
    """
    user = kwargs['user']
    possible_statuses = DEPLOYED_STATUSES + DRAFT_STATUSES
    if status not in possible_statuses:
        return make_api_response("",
                                 f"You cannot apply the status {status} on yara rules.",
                                 403)

    data = STORAGE.signature.get(sid, as_obj=False)
    if data:
        if not Classification.is_accessible(user['classification'],
                                            data.get('classification', Classification.UNRESTRICTED)):
            return make_api_response("", "You are not allowed change status on this signature", 403)
    
        if data['status'] in STALE_STATUSES and status not in DRAFT_STATUSES:
            return make_api_response("",
                                     f"Only action available while signature in {data['status']} "
                                     f"status is to change signature to a DRAFT status. ({', '.join(DRAFT_STATUSES)})",
                                     403)

        if data['status'] in DEPLOYED_STATUSES and status in DRAFT_STATUSES:
            return make_api_response("",
                                     f"You cannot change the status of signature {sid} from "
                                     f"{data['status']} to {status}.", 403)

        query = f"status:{status} AND signature_id:{data['signature_id']} AND NOT id:{sid}"
        today = now_as_iso()
        uname = user['uname']

        if status not in ['DISABLED', 'INVALID', 'TESTING']:
            keys = [k['id']
                    for k in STORAGE.signature.search(query, fl="id", as_obj=False)['items']]
            for other in STORAGE.signature.multiget(keys, as_obj=False, as_dictionary=False):
                other['state_change_date'] = today
                other['state_change_user'] = uname
                other['status'] = 'DISABLED'

                STORAGE.signature.save(f"{other['meta']['rule_id']}r.{other['meta']['rule_version']}", other)

        data['state_change_date'] = today
        data['state_change_user'] = uname
        data['status'] = status

        return make_api_response({"success": STORAGE.signature.save(sid, data)})
    else:
        return make_api_response("", f"Signature not found. ({sid})", 404)


@signature_api.route("/<sid>/", methods=["DELETE"])
@api_login(required_priv=['W'], allow_readonly=False, require_type=['admin', 'signature_manager'])
def delete_signature(sid, **kwargs):
    """
    Delete a signature based of its ID

    Variables:
    sid    =>     Signature ID

    Arguments:
    None

    Data Block:
    None

    Result example:
    {"success": True}  # Signature delete successful
    """
    user = kwargs['user']
    data = STORAGE.signature.get(sid, as_obj=False)
    if data:
        if not Classification.is_accessible(user['classification'],
                                            data.get('classification', Classification.UNRESTRICTED)):
            return make_api_response("", "Your are not allowed to delete this signature.", 403)
        return make_api_response({"success": STORAGE.signature.delete(sid)})
    else:
        return make_api_response("", f"Signature not found. ({sid})", 404)


@signature_api.route("/sources/<service>/<name>/", methods=["DELETE"])
@api_login(audit=False, required_priv=['W'], allow_readonly=False, require_type=['admin', 'signature_manager'])
def delete_signature_source(service, name, **_):
    """
    Delete a signature source by name for a given service

    Variables:
    service           =>      Service to which we want to delete the source from
    name              =>      Name of the source you want to remove

    Arguments:
    None

    Data Block:
    None

    Result example:
    {"success": True/False}   # if the operation succeeded of not
    """
    service_data = STORAGE.get_service_with_delta(service, as_obj=False)
    current_sources = service_data.get('update_config', {}).get('sources', [])

    if not service_data.get('update_config', {}).get('generates_signatures', False):
        return make_api_response({"success": False},
                                 err="This service does not generate alerts therefor "
                                     "you cannot delete one of its sources.",
                                 status_code=400)

    new_sources = []
    found = False
    for source in current_sources:
        if name == source['name']:
            found = True
        else:
            new_sources.append(source)

    if not found:
        return make_api_response({"success": False},
                                 err=f"Could not found source '{name}' in service {service}.",
                                 status_code=404)

    service_delta = STORAGE.service_delta.get(service, as_obj=False)
    if service_delta.get('update_config') is None:
        service_delta['update_config'] = {"sources": new_sources}
    else:
        service_delta['update_config']['sources'] = new_sources

    # Save the signature
    return make_api_response({"success": STORAGE.service_delta.save(service, service_delta)})


# noinspection PyBroadException
def _get_cached_signatures(signature_cache, query_hash):
    try:
        s = signature_cache.get(query_hash)
        if s is None:
            return s
        return make_file_response(
            s, f"al_signatures_{query_hash[:7]}.zip", len(s), content_type="application/zip"
        )
    except Exception:  # pylint: disable=W0702
        LOGGER.exception('Failed to read cached signatures:')

    return None


@signature_api.route("/download/", methods=["GET"])
@api_login(required_priv=['R'], check_xsrf_token=False, allow_readonly=False,
           require_type=['signature_importer', 'user'])
def download_signatures(**kwargs):
    """
    Download signatures from the system.
    
    Variables:
    None 
    
    Arguments: 
    query       => Query used to filter the signatures
                   Default: All deployed signatures

    Data Block:
    None
    
    Result example:
    <A zip file containing all signatures files from the different sources>
    """
    user = kwargs['user']
    query = request.args.get('query', 'status:DEPLOYED')

    access = user['access_control']
    last_modified = STORAGE.get_signature_last_modified()

    query_hash = sha256(f'{query}.{access}.{last_modified}'.encode('utf-8')).hexdigest()

    with forge.get_cachestore('al_ui.signature') as signature_cache:
        response = _get_cached_signatures(signature_cache, query_hash)
        if response:
            return response

        with Lock(f"al_signatures_{query_hash[:7]}.zip", 30):
            response = _get_cached_signatures(signature_cache, query_hash)
            if response:
                return response

            output_files = {}

            keys = [k['id']
                    for k in STORAGE.signature.stream_search(query, fl="id", access_control=access, as_obj=False)]
            signature_list = sorted(STORAGE.signature.multiget(keys, as_dictionary=False, as_obj=False),
                                    key=lambda x: x['order'])

            for sig in signature_list:
                out_fname = f"{sig['type']}/{sig['source']}"
                output_files.setdefault(out_fname, [])
                output_files[out_fname].append(sig['data'])

            output_zip = InMemoryZip()
            for fname, data in output_files.items():
                output_zip.append(fname, "\n\n".join(data))

            rule_file_bin = output_zip.read()

            signature_cache.save(query_hash, rule_file_bin, ttl=DEFAULT_CACHE_TTL)

            return make_file_response(
                rule_file_bin, f"al_signatures_{query_hash[:7]}.zip",
                len(rule_file_bin), content_type="application/zip"
            )


@signature_api.route("/<sid>/", methods=["GET"])
@api_login(required_priv=['R'], allow_readonly=False)
def get_signature(sid, **kwargs):
    """
    Get the detail of a signature based of its ID and revision
    
    Variables:
    sid    =>     Signature ID
    
    Arguments: 
    None
    
    Data Block:
    None
     
    Result example:
    {}
    """
    user = kwargs['user']
    data = STORAGE.signature.get(sid, as_obj=False)

    if data:
        if not Classification.is_accessible(user['classification'],
                                            data.get('classification', Classification.UNRESTRICTED)):
            return make_api_response("", "Your are not allowed to view this signature.", 403)

        return make_api_response(data)
    else:
        return make_api_response("", f"Signature not found. ({sid})", 404)


@signature_api.route("/sources/", methods=["GET"])
@api_login(audit=False, required_priv=['R'], allow_readonly=False, require_type=['admin', 'signature_manager'])
def get_signature_sources(**_):
    """
    Get all signature sources

    Variables:
    None

    Arguments:
    None

    Data Block:
    None

    Result example:
    {
     'Yara': {
        {
          "uri": "http://somesite/file_to_get",   # URI to fetch for parsing the rules
          "name": "signature_file.yar",           # Name of the file we will parse the rules as
          "username": null,                       # Username used to get to the URI
          "password": null,                       # Password used to get to the URI
          "header": {                             # Header sent during the request to the URI
            "X_TOKEN": "SOME RANDOM TOKEN"          # Exemple of header
          },
          "public_key": null,                     # Public key used to get to the URI
          "pattern": "^*.yar$"                    # Regex pattern use to get appropriate files from the URI
        }, ...
      }, ...
    }
    """
    services = STORAGE.list_all_services(full=True, as_obj=False)

    out = {}
    for service in services:
        if service.get("update_config", {}).get("generates_signatures", False):
            out[service['name']] = service['update_config']['sources']

    # Save the signature
    return make_api_response(out)


@signature_api.route("/<sid>/", methods=["POST"])
@api_login(required_priv=['W'], allow_readonly=False, require_type=['signature_importer'])
def update_signature(sid, **_):
    """
    Update a signature defined by a sid and a rev.
       NOTE: The API will compare the old signature
             with the new one and will make the decision
             to increment the revision number or not. 
    
    Variables:
    sid    =>     Signature ID

    Arguments: 
    None
    
    Data Block (REQUIRED): # Signature block
    {"name": "sig_name",           # Signature name
     "type": "yara",               # One of yara, suricata or tagcheck
     "data": "rule sample {...}",  # Data of the rule to be added
     "source": "yara_signatures"   # Source from where the signature has been gathered
    }

    Result example:
    {"success": true,      #If saving the rule was a success or not
     "id": "<TYPE>_<SID>_<REVISION>"}  #ID that was assigned to the signature
    """
    # Get old signature
    old_data = STORAGE.signature.get(sid, as_obj=False)
    if old_data:
        data = request.json
        return make_api_response({"success": STORAGE.signature.save(sid, data),
                                  "sid": sid})
    else:
        return make_api_response({"success": False}, "Signature not found. %s" % sid, 404)


@signature_api.route("/sources/<service>/<name>/", methods=["POST"])
@api_login(audit=False, required_priv=['W'], allow_readonly=False, require_type=['admin', 'signature_manager'])
def update_signature_source(service, name, **_):
    """
    Update a signature source by name for a given service

    Variables:
    service           =>      Service to which we want to update the source
    name              =>      Name of the source you want update

    Arguments:
    None

    Data Block:
    {
      "uri": "http://somesite/file_to_get",   # URI to fetch for parsing the rules
      "name": "signature_file.yar",           # Name of the file we will parse the rules as
      "username": null,                       # Username used to get to the URI
      "password": null,                       # Password used to get to the URI
      "header": {                             # Header sent during the request to the URI
        "X_TOKEN": "SOME RANDOM TOKEN"          # Exemple of header
      },
      "public_key": null,                     # Public key used to get to the URI
      "pattern": "^*.yar$"                    # Regex pattern use to get appropriate files from the URI
    }

    Result example:
    {"success": True/False}   # if the operation succeeded of not
    """
    data = request.json
    service_data = STORAGE.get_service_with_delta(service, as_obj=False)
    current_sources = service_data.get('update_config', {}).get('sources', [])

    if name != data['name']:
        return make_api_response({"success": False},
                                 err="You are not allowed to change the source resulting filename.",
                                 status_code=400)

    if not service_data.get('update_config', {}).get('generates_signatures', False):
        return make_api_response({"success": False},
                                 err="This service does not generate alerts therefor you cannot update its sources.",
                                 status_code=400)

    if len(current_sources) == 0:
        return make_api_response({"success": False},
                                 err="This service does not have any sources therefor you cannot update any source.",
                                 status_code=400)

    new_sources = []
    found = False
    for source in current_sources:
        if data['name'] == source['name']:
            new_sources.append(data)
            found = True
        else:
            new_sources.append(source)

    if not found:
        return make_api_response({"success": False},
                                 err=f"Could not found source '{data.name}' in service {service}.",
                                 status_code=404)

    service_delta = STORAGE.service_delta.get(service, as_obj=False)
    if service_delta.get('update_config') is None:
        service_delta['update_config'] = {"sources": new_sources}
    else:
        service_delta['update_config']['sources'] = new_sources

    # Save the signature
    return make_api_response({"success": STORAGE.service_delta.save(service, service_delta)})


@signature_api.route("/stats/", methods=["GET"])
@api_login(allow_readonly=False)
def signature_statistics(**kwargs):
    """
    Gather all signatures stats in system

    Variables:
    None

    Arguments:
    None

    Data Block:
    None

    Result example:
    [                             # List of signature stats
      {"sid": "ORG_000000",          # Signature ID
       "rev": 1,                     # Signature version
       "classification": "U",        # Classification of the signature
       "name": "Signature Name"      # Signature name
       "count": "100",               # Count of times signatures seen
       "min": 0,                     # Lowest score found
       "avg": 172,                   # Average of all scores
       "max": 780,                   # Highest score found
      },
     ...
    ]"""

    user = kwargs['user']

    def get_stat_for_signature(p_id, p_source, p_name, p_type, p_classification):
        stats = STORAGE.result.stats("result.score",
                                     query=f'result.sections.tags.file.rule.{p_type}:"{p_source}.{p_name}"')
        if stats['count'] == 0:
            return {
                'id': p_id,
                'source': p_source,
                'name': p_name,
                'type': p_type,
                'classification': p_classification,
                'count': stats['count'],
                'min': 0,
                'max': 0,
                'avg': 0,
            }
        else:
            return {
                'id': p_id,
                'source': p_source,
                'name': p_name,
                'type': p_type,
                'classification': p_classification,
                'count': stats['count'],
                'min': int(stats['min']),
                'max': int(stats['max']),
                'avg': int(stats['avg']),
            }

    sig_list = sorted([(x['id'], x['source'], x['name'], x['type'], x['classification'])
                       for x in STORAGE.signature.stream_search("name:*",
                                                                fl="id,name,type,source,classification",
                                                                access_control=user['access_control'], as_obj=False)])

    with concurrent.futures.ThreadPoolExecutor(max(min(len(sig_list), 20), 1)) as executor:
        res = [executor.submit(get_stat_for_signature, sid, source, name, sig_type, classification)
               for sid, source, name, sig_type, classification in sig_list]

    return make_api_response(sorted([r.result() for r in res], key=lambda i: i['type']))


@signature_api.route("/update_available/", methods=["GET"])
@api_login(required_priv=['R'], allow_readonly=False,
           require_type=['signature_importer', 'user'])
def update_available(**_):
    """
    Check if updated signatures are.

    Variables:
    None

    Arguments:
    last_update        => ISO time of last update.
    type               => Signature type to check

    Data Block:
    None

    Result example:
    { "update_available" : true }      # If updated rules are available.
    """
    sig_type = request.args.get('type', '*')
    last_update = iso_to_epoch(request.args.get('last_update', '1970-01-01T00:00:00.000000Z'))
    last_modified = iso_to_epoch(STORAGE.get_signature_last_modified(sig_type))

    return make_api_response({"update_available": last_modified > last_update})


@signature_api.route("/add_update/", methods=["POST"])
@api_login(audit=False, required_priv=['W'], allow_readonly=False, require_type=['signature_importer'])
def add_update_signature(**_):
    """
    Variables:
    None

    Arguments:
    None

    Data Block (REQUIRED): # Signature block
    {"name": "sig_name",           # Signature name
     "type": "yara",               # One of yara, suricata or tagcheck
     "data": "rule sample {...}",  # Data of the rule to be added
     "source": "yara_signatures"   # Source from where the signature has been gathered
    }

    Result example:
    {"success": true,      #If saving the rule was a success or not
     "id": "<TYPE>_<SID>_<REVISION>"}  #ID that was assigned to the signature
    """
    data = request.json

    if data.get('type', None) is None or data['name'] is None or data['data'] is None:
        return make_api_response("", f"Signature id, name, type and data are mandatory fields.", 400)

    # Compute signature ID if missing
    data['signature_id'] = data.get('signature_id', f"{data['source']}.{data['name']}")

    # Save the signature
    return make_api_response({"success": STORAGE.signature.save(data['signature_id'], data),
                              "id": data['signature_id']})