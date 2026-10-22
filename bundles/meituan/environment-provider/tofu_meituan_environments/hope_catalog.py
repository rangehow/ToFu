"""Read one trusted actor/project queue; never expose apps, credentials or image config."""
from tofu_environments.contracts import EnvironmentError
from tofu_environments.capacity import queue_resources

def read_catalog(provider, actor, profile):
    if not profile or not all(isinstance(profile.get(k), str) and profile[k] for k in ['queue','usergroup']):
        raise EnvironmentError('hope_catalog_unconfigured', 'Trusted queue and account profile required')
    response = provider._call(actor, 'get_queue_resource', {'queue':profile['queue'], 'include_apps':False})
    if response.get('queue') != profile['queue'] or not isinstance(response.get('summary'), dict):
        raise EnvironmentError('hope_catalog_invalid', 'Queue identity and summary required')
    summary = response['summary']
    resources = []
    try:
        gpu = summary.get('gpu', {})
        if not isinstance(gpu, dict) or len(gpu) > 62:
            raise ValueError('GPU catalog shape')
        resources = queue_resources(response, profile['queue'])
        if any(row[key] is None for row in resources for key in ('total', 'allocated', 'available')):
            raise ValueError('Queue resource summary is incomplete')
    except (KeyError, TypeError, ValueError) as exc:
        raise EnvironmentError('hope_catalog_invalid', 'Queue resource summary is incomplete') from exc
    options = {'type':'object','additionalProperties':False,
               'required':['usergroup','job_type','queue','priority'],
               'properties':{key:{'type':'string','minLength':1,'maxLength':256}
                             for key in ['usergroup','job_type','queue','priority','resource_code','conf_name']},
               'allOf':[{'if':{'properties':{'priority':{'const':'P0'}},'required':['priority']},
                         'then':{'required':['resource_code']}}]}
    options['properties']['priority']['enum'] = ['P0','P1']
    return {'version':1,'provider':'hope','queues':[{'name':profile['queue'],'resources':resources}],
            'provider_options_schema':options,
            'provider_options_defaults':{'usergroup':profile['usergroup'],'queue':profile['queue'],'job_type':'ml-easy-job'},
            'availability_is_admission_guarantee':False}
