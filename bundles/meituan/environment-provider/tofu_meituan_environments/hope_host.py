"""Lazy operator-owned Hope profile loading; no import-time I/O or login.

Profiles live under data/config/compute/hope/<owner-tenant-scope>.json. The
existing explicit-owner MCP store remains the connection credential authority.
"""
import json
import os
from pathlib import Path
import stat
from tofu_environments.contracts import EnvironmentError


def _read_profile(path):
    try:
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        try:
            metadata=os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode&0o077 or not 0<metadata.st_size<=65536:
                raise EnvironmentError('hope_profile_invalid','Private bounded regular profile required')
            with os.fdopen(fd,'rb',closefd=False) as stream:raw=stream.read(65537)
            if len(raw)>65536:raise EnvironmentError('hope_profile_invalid','Profile byte limit exceeded')
        finally:os.close(fd)
        def pairs(items):
            result={}
            for key,value in items:
                if key in result:raise ValueError('duplicate key')
                result[key]=value
            return result
        return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite')))
    except FileNotFoundError as exc:
        raise EnvironmentError('hope_profile_missing','Configure an owned Hope compute profile before allocation') from exc
    except EnvironmentError:raise
    except (OSError,ValueError,UnicodeError) as exc:
        raise EnvironmentError('hope_profile_invalid','Owned Hope profile cannot be read') from exc


def load_host_binding(actor):
    from lib.runtime.runtime_paths import data_root
    from lib.mcp.config import load_mcp_config
    document=_read_profile(Path(data_root())/'config/compute/hope'/f'{actor.scope}.json')
    from jsonschema import Draft202012Validator
    schema=json.loads(Path(__file__).with_name('hope_profile.schema.json').read_text())
    if not Draft202012Validator(schema).is_valid(document):
        raise EnvironmentError('hope_profile_invalid','Complete version1 Hope profile required')
    if type(document['owner_id']) is not int or document['owner_id']!=actor.owner_id or document['tenant_id']!=actor.tenant_id:
        raise EnvironmentError('hope_scope_mismatch','Compute profile belongs to another owner/tenant')
    projects=document['projects']
    if not isinstance(projects,list) or not 0<len(projects)<=256 or not all(isinstance(p,str) and Path(p).is_absolute() for p in projects):
        raise EnvironmentError('hope_profile_invalid','Explicit absolute allowed projects required')
    if not actor.project_root or Path(actor.project_root).resolve() not in {Path(p).resolve() for p in projects}:
        raise EnvironmentError('hope_scope_mismatch','Compute profile is not bound to this project')
    config=load_mcp_config(user_id=actor.owner_id).get('hope')
    if not isinstance(config,dict) or not config or config.get('enabled') is False:
        raise EnvironmentError('hope_unconfigured','Enabled owner-bound Hope MCP configuration required')
    return {'scope':actor.scope,'project_root':actor.project_root,
            **{k:document[k] for k in ('profile','artifact_profile','expected_creator','environment','mcp_timeout_seconds','output_timeout_seconds')},
            'mcp_configuration':config}
