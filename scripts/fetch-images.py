"""Fetch official Docker Hub linux/amd64 images without a Docker daemon.

Creates a docker-load archive and a digest manifest. All blobs and expanded
layer diff IDs are checked; no third-party registry mirror is used.
"""
import concurrent.futures
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tarfile
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ACCEPT = ', '.join(['application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json', 'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'])

def session():
    s = requests.Session()
    s.mount('https://', HTTPAdapter(max_retries=Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])))
    return s

def digest(path):
    with path.open('rb') as f:
        return 'sha256:' + hashlib.file_digest(f, 'sha256').hexdigest()

def main(repo, tag, dest):
    target = Path(dest).resolve()
    target.mkdir(parents=True, exist_ok=True)
    s = session()
    token_response = s.get('https://auth.docker.io/token', params={'service':'registry.docker.io', 'scope':f'repository:{repo}:pull'}, timeout=45)
    token_response.raise_for_status()
    headers = {'Authorization':'Bearer ' + token_response.json()['token'], 'Accept':ACCEPT}
    base = f'https://registry-1.docker.io/v2/{repo}'
    def manifest(ref):
        r = s.get(base + '/manifests/' + ref, headers=headers, timeout=60)
        r.raise_for_status()
        actual = 'sha256:' + hashlib.sha256(r.content).hexdigest()
        if ref.startswith('sha256:') and actual != ref:
            raise ValueError('Manifest digest mismatch')
        return r.json(), actual
    data, index_digest = manifest(tag)
    if 'manifests' in data:
        candidates = [m for m in data['manifests'] if m.get('platform', {}).get('os') == 'linux' and m.get('platform', {}).get('architecture') == 'amd64']
        if len(candidates) != 1:
            raise ValueError('Ambiguous linux/amd64 manifest')
        data, manifest_digest = manifest(candidates[0]['digest'])
    else:
        manifest_digest = index_digest
    def blob(desc):
        path = target / desc['digest'].split(':')[1]
        if path.exists() and digest(path) == desc['digest']:
            return path
        with session().get(base + '/blobs/' + desc['digest'], headers=headers, timeout=(30, 120), stream=True) as r:
            r.raise_for_status()
            with path.with_suffix('.partial').open('wb') as out:
                for chunk in r.iter_content(1024 * 1024):
                    out.write(chunk)
        if digest(path.with_suffix('.partial')) != desc['digest']:
            raise ValueError('Blob digest mismatch')
        path.with_suffix('.partial').replace(path)
        return path
    config_path = blob(data['config'])
    config = json.loads(config_path.read_bytes())
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        layers = list(pool.map(blob, data['layers']))
    layer_names = []
    for i, path in enumerate(layers):
        expanded = path.with_suffix('.tar')
        if not expanded.exists():
            with gzip.open(path, 'rb') as source, expanded.open('wb') as out:
                shutil.copyfileobj(source, out)
        if digest(expanded) != config['rootfs']['diff_ids'][i]:
            raise ValueError('Expanded layer digest mismatch')
        layer_names.append(expanded.name)
    config_name = config_path.name + '.json'
    shutil.copyfile(config_path, target / config_name)
    repo_tag = repo + (':verified-' + manifest_digest.split(':')[1][:12] if tag.startswith('sha256:') else ':' + tag)
    docker_manifest = [{'Config':config_name, 'RepoTags':[repo_tag], 'Layers':layer_names}]
    manifest_path = target / 'manifest.json'
    manifest_path.write_text(json.dumps(docker_manifest), encoding='utf-8')
    archive = target / 'image.tar.gz'
    with tarfile.open(archive, 'w:gz', compresslevel=1) as tar:
        for name in ['manifest.json', config_name] + layer_names:
            tar.add(target / name, arcname=name)
    record = {'reference':repo_tag, 'index_digest':index_digest, 'manifest_digest':manifest_digest, 'image_id':data['config']['digest'], 'archive_sha256':digest(archive)}
    (target / 'provenance.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(record), flush=True)

if __name__ == '__main__':
    main(*sys.argv[1:])
