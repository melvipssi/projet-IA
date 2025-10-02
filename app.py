#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AWS Infra Helper — Single File Edition
Features:
- S3: list/create/upload/delete
- EC2: list/launch (public, with nginx via user-data)
- GitHub: clone owner/repo or full URL and preview static index.html
Run:
  pip install Flask boto3 GitPython
  export AWS_REGION=eu-west-3
  python aws_infra_helper_single.py
"""
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, abort, Response, send_from_directory
import boto3
from botocore.exceptions import ClientError

try:
    from git import Repo
    GIT_OK = True
except Exception:
    import subprocess
    GIT_OK = False

APP_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "eu-west-3"
REPOS_BASE = Path(os.environ.get("REPOS_BASE", "repos")).resolve()
REPOS_BASE.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

# --- AWS clients ---
session = boto3.Session(region_name=APP_REGION)
s3 = session.client("s3")
ec2 = session.client("ec2")
ssm = session.client("ssm")

# --- Helpers ---
def _serialize_dt(dt):
    return dt.isoformat() if isinstance(dt, datetime) else dt

def _bucket_region(bucket):
    try:
        r = s3.get_bucket_location(Bucket=bucket)
        loc = r.get("LocationConstraint")
        return "us-east-1" if loc in (None, "") else loc
    except ClientError as e:
        return f"unknown ({e.response.get('Error', {}).get('Code', 'err')})"

def _empty_bucket(bucket):
    # delete versions and delete markers (if versioning)
    try:
        paginator = s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket):
            objs = [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in page.get("Versions", [])]
            dms = [{"Key": m["Key"], "VersionId": m["VersionId"]} for m in page.get("DeleteMarkers", [])]
            to_del = objs + dms
            if to_del:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": to_del})
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code not in ("NoSuchVersion", "InvalidArgument", "NoSuchBucket"):
            raise
    # delete current objects
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        objs = [{"Key": x["Key"]} for x in page.get("Contents", [])]
        if objs:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objs})

def _default_vpc_and_subnet():
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
    if not vpcs["Vpcs"]:
        raise RuntimeError("No default VPC found")
    vpc_id = vpcs["Vpcs"][0]["VpcId"]
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}, {"Name": "default-for-az", "Values": ["true"]}])
    if not subnets["Subnets"]:
        subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])
    subnet_id = subnets["Subnets"][0]["SubnetId"]
    return vpc_id, subnet_id

def _ensure_sg(vpc_id, name="infra-tool-sg"):
    r = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [name]}, {"Name": "vpc-id", "Values": [vpc_id]}])
    if r["SecurityGroups"]:
        sg_id = r["SecurityGroups"][0]["GroupId"]
    else:
        sg = ec2.create_security_group(GroupName=name, Description="Managed by Infra Tool", VpcId=vpc_id)
        sg_id = sg["GroupId"]
    # Ensure ingress 22,80 open (ignore duplicates)
    try:
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
                {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
            ],
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in ("InvalidPermission.Duplicate", "InvalidGroup.Duplicate"):
            raise
    return sg_id

def _latest_al2023_ami():
    try:
        p = ssm.get_parameter(Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64")
        return p["Parameter"]["Value"]
    except ClientError:
        imgs = ec2.describe_images(
            Owners=["amazon"],
            Filters=[
                {"Name": "name", "Values": ["al2023-ami-*-x86_64"]},
                {"Name": "architecture", "Values": ["x86_64"]},
                {"Name": "state", "Values": ["available"]},
            ]
        )["Images"]
        imgs.sort(key=lambda x: x.get("CreationDate", ""), reverse=True)
        if not imgs:
            raise RuntimeError("Could not find an Amazon Linux 2023 AMI")
        return imgs[0]["ImageId"]

# --- UI (single-file: inline HTML/CSS/JS) ---
INDEX_HTML = r"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AWS Infra Helper — Single File</title>
  <style>
    *{box-sizing:border-box}body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#0b1020;color:#e6ebff}
    header{padding:24px 16px;background:linear-gradient(90deg,#111a3a,#0f1530);border-bottom:1px solid #222a55}
    h1{margin:0;font-size:24px}h2{margin:0 0 12px 0;font-size:18px}
    main{max-width:1100px;margin:0 auto;padding:16px;display:grid;gap:16px}
    .card{background:#101838;border:1px solid #1b2450;border-radius:14px;padding:16px;box-shadow:0 8px 24px rgba(0,0,0,.25)}
    .form-row{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
    input[type=text], input[type=file], input{background:#0c1330;border:1px solid #2a3470;color:#dbe2ff;border-radius:10px;padding:10px;min-width:220px}
    button{background:#4c67ff;border:0;color:white;padding:10px 14px;border-radius:10px;cursor:pointer}
    button:hover{opacity:.9} .actions{margin-bottom:8px}
    .table{width:100%;border-collapse:collapse;margin-top:8px;font-size:14px}
    .table th,.table td{border-bottom:1px solid #2a3470;padding:8px;text-align:left}
    .badge{display:inline-block;padding:2px 8px;border-radius:999px;background:#1b2450}
    footer{max-width:1100px;margin:0 auto;padding:24px 16px;color:#97a2ff;opacity:.8}
    a{color:#8fb1ff}
  </style>
</head>
<body>
<header>
  <h1>AWS Infra Helper — Single File</h1>
  <p style="opacity:.8;margin:6px 0 0 0;">S3 • EC2 • GitHub — via rôle IAM.</p>
</header>
<main>
  <section class="card">
    <h2>S3 — Buckets</h2>
    <div class="actions"><button id="refreshBuckets">Lister les buckets</button></div>
    <div class="form-row">
      <input id="newBucketName" placeholder="Nom du bucket (unique mondialement)">
      <input id="newBucketRegion" placeholder="Région (ex: eu-west-3)" value="eu-west-3">
      <button id="createBucket">Créer</button>
    </div>
    <div class="form-row">
      <input id="uploadBucket" placeholder="Bucket pour upload">
      <input id="uploadPrefix" placeholder="Préfixe (optionnel dossier/)">
      <input type="file" id="uploadFile">
      <button id="uploadBtn">Uploader</button>
    </div>
    <div id="buckets"></div>
  </section>

  <section class="card">
    <h2>EC2 — Instances</h2>
    <div class="actions"><button id="refreshInstances">Lister les instances</button></div>
    <div class="form-row">
      <input id="ec2Name" placeholder="Tag Name" value="infra-tool-ec2">
      <input id="ec2Type" placeholder="Type (ex: t3.micro)" value="t3.micro">
      <input id="ec2Key" placeholder="Key pair (optionnel)">
      <input id="ec2Ami" placeholder="AMI (vide = AL2023 latest)">
      <button id="launchEc2">Lancer une instance publique</button>
    </div>
    <div id="instances"></div>
  </section>

  <section class="card">
    <h2>GitHub — Cloner et prévisualiser</h2>
    <div class="form-row">
      <input id="repoUrl" placeholder="owner/repo ou URL https">
      <button id="cloneRepo">Cloner</button>
    </div>
    <div id="repos"></div>
  </section>
</main>
<footer><small>⚙️ Flask + Boto3 • Pense à attacher un rôle IAM sur l’EC2</small></footer>

<script>
async function api(path, options={}){
  const r = await fetch(path, options);
  const data = await r.json().catch(()=>({}));
  if(!r.ok){ throw new Error(data.error || r.statusText); }
  return data;
}
function el(tag, attrs={}, children=[]) {
  const e = document.createElement(tag);
  Object.entries(attrs).forEach(([k,v]) => e.setAttribute(k, v));
  (Array.isArray(children) ? children : [children]).filter(Boolean).forEach(c => {
    if (typeof c === 'string') e.appendChild(document.createTextNode(c));
    else e.appendChild(c);
  });
  return e;
}

// S3
const bucketsDiv = document.getElementById('buckets');
async function refreshBuckets(){
  bucketsDiv.innerHTML = 'Chargement...';
  try{
    const b = await api('/api/s3');
    const table = el('table', {class:'table'});
    table.appendChild(el('thead',{},[ el('tr',{},[
      el('th',{},'Nom'), el('th',{},'Région'), el('th',{},'Créé'), el('th',{},'Actions')
    ]) ]));
    const tbody = el('tbody');
    b.forEach(x => {
      const tr = el('tr',{},[
        el('td',{}, x.name),
        el('td',{}, x.region),
        el('td',{}, new Date(x.creationDate).toLocaleString()),
        el('td',{}, (()=>{
          const btn = el('button',{},'Supprimer');
          btn.onclick = async () => {
            if (!confirm(`Supprimer le bucket ${x.name} ? (vide d'abord)`)) return;
            try{ await api(`/api/s3/${x.name}`, {method:'DELETE'}); refreshBuckets(); }
            catch(e){ alert(e.message); }
          };
          return btn;
        })())
      ]);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    bucketsDiv.innerHTML=''; bucketsDiv.appendChild(table);
  }catch(e){ bucketsDiv.innerHTML = 'Erreur: ' + e.message; }
}
document.getElementById('refreshBuckets').onclick = refreshBuckets;

document.getElementById('createBucket').onclick = async ()=>{
  const name = document.getElementById('newBucketName').value.trim();
  const region = document.getElementById('newBucketRegion').value.trim() || 'eu-west-3';
  if(!name) return alert('Nom requis');
  try{ await api('/api/s3', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({bucket_name:name, region})}); refreshBuckets(); }
  catch(e){ alert(e.message); }
};

document.getElementById('uploadBtn').onclick = async ()=>{
  const bucket = document.getElementById('uploadBucket').value.trim();
  const prefix = document.getElementById('uploadPrefix').value.trim();
  const file = document.getElementById('uploadFile').files[0];
  if(!bucket || !file) return alert('Bucket et fichier requis');
  const fd = new FormData(); fd.append('bucket', bucket); fd.append('prefix', prefix); fd.append('file', file);
  try{ const res = await api('/api/s3/upload', {method:'POST', body: fd}); alert('Upload OK: ' + res.key); }
  catch(e){ alert(e.message); }
};

// EC2
const instancesDiv = document.getElementById('instances');
async function refreshInstances(){
  instancesDiv.innerHTML = 'Chargement...';
  try{
    const data = await api('/api/ec2');
    const table = el('table', {class:'table'});
    table.appendChild(el('thead',{},[ el('tr',{},[
      el('th',{},'ID'), el('th',{},'Nom'), el('th',{},'Type'), el('th',{},'État'), el('th',{},'Public IP'), el('th',{},'Lancé')
    ]) ]));
    const tbody = el('tbody');
    data.forEach(i => {
      tbody.appendChild(el('tr',{},[
        el('td',{}, i.instanceId),
        el('td',{}, i.name || '-'),
        el('td',{}, i.type),
        el('td',{}, el('span', {class:'badge'}, i.state)),
        el('td',{}, i.publicIp || '-'),
        el('td',{}, new Date(i.launchTime).toLocaleString()),
      ]));
    });
    table.appendChild(tbody);
    instancesDiv.innerHTML=''; instancesDiv.appendChild(table);
  }catch(e){ instancesDiv.innerHTML = 'Erreur: ' + e.message; }
}
document.getElementById('refreshInstances').onclick = refreshInstances;

document.getElementById('launchEc2').onclick = async ()=>{
  const name = document.getElementById('ec2Name').value.trim();
  const itype = document.getElementById('ec2Type').value.trim() || 't3.micro';
  const key = document.getElementById('ec2Key').value.trim();
  const ami = document.getElementById('ec2Ami').value.trim();
  try{
    const body = {name, instance_type: itype};
    if (key) body.key_name = key;
    if (ami) body.ami_id = ami;
    const res = await api('/api/ec2/launch', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    alert('Instance lancée: ' + res.instanceId); refreshInstances();
  }catch(e){ alert(e.message); }
};

// GitHub
const reposDiv = document.getElementById('repos');
document.getElementById('cloneRepo').onclick = async ()=>{
  const url = document.getElementById('repoUrl').value.trim();
  if(!url) return alert('URL ou owner/repo requis');
  try{
    const res = await api('/api/repo/clone', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url})});
    const a = document.createElement('a'); a.href = res.previewUrl; a.target = '_blank'; a.textContent = res.previewUrl;
    reposDiv.innerHTML = ''; reposDiv.appendChild(document.createTextNode('Cloné: ' + res.name + ' — prévisualisation: ')); reposDiv.appendChild(a);
  }catch(e){ alert(e.message); }
};
</script>
</body></html>
"""

@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")

# --- S3 endpoints ---
@app.get("/api/s3")
def list_buckets():
    r = s3.list_buckets()
    out = []
    for b in r.get("Buckets", []):
        out.append({
            "name": b["Name"],
            "creationDate": _serialize_dt(b["CreationDate"]),
            "region": _bucket_region(b["Name"]),
        })
    return jsonify(out)

@app.post("/api/s3")
def create_bucket():
    data = request.get_json(force=True, silent=True) or {}
    name = data.get("bucket_name")
    region = data.get("region") or APP_REGION
    if not name:
        return jsonify({"error": "bucket_name required"}), 400
    try:
        if region == "us-east-1":
            s3.create_bucket(Bucket=name)
        else:
            s3.create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": region})
        return jsonify({"ok": True, "bucket": name, "region": region})
    except ClientError as e:
        return jsonify({"error": str(e)}), 400

@app.post("/api/s3/upload")
def upload_to_bucket():
    bucket = request.form.get("bucket")
    prefix = request.form.get("prefix", "").strip()
    f = request.files.get("file")
    if not bucket or not f:
        return jsonify({"error": "bucket and file are required"}), 400
    key = re.sub(r"[^\w\-. /]", "_", f.filename)
    if prefix:
        prefix = prefix.strip("/")
        key = f"{prefix}/{key}"
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=f.stream.read())
        url = f"https://{bucket}.s3.amazonaws.com/{key}"
        return jsonify({"ok": True, "bucket": bucket, "key": key, "url": url})
    except ClientError as e:
        return jsonify({"error": str(e)}), 400

@app.delete("/api/s3/<bucket>")
def delete_bucket(bucket):
    try:
        _empty_bucket(bucket)
        s3.delete_bucket(Bucket=bucket)
        return jsonify({"ok": True, "bucket": bucket})
    except ClientError as e:
        return jsonify({"error": str(e)}), 400

# --- EC2 endpoints ---
@app.get("/api/ec2")
def list_instances():
    instances = []
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate():
        for r in page["Reservations"]:
            for i in r.get("Instances", []):
                name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), None)
                instances.append({
                    "instanceId": i["InstanceId"],
                    "type": i.get("InstanceType"),
                    "state": i.get("State", {}).get("Name"),
                    "publicIp": i.get("PublicIpAddress"),
                    "privateIp": i.get("PrivateIpAddress"),
                    "name": name,
                    "launchTime": _serialize_dt(i.get("LaunchTime")),
                    "az": i.get("Placement", {}).get("AvailabilityZone"),
                })
    return jsonify(instances)

@app.post("/api/ec2/launch")
def launch_instance():
    data = request.get_json(force=True, silent=True) or {}
    instance_type = data.get("instance_type", "t3.micro")
    key_name = data.get("key_name")  # optional
    ami_id = data.get("ami_id") or _latest_al2023_ami()
    name_tag = data.get("name", "infra-tool-ec2")
    try:
        vpc_id, subnet_id = _default_vpc_and_subnet()
        sg_id = _ensure_sg(vpc_id)
        user_data = """#cloud-config
package_update: true
packages:
  - nginx
runcmd:
  - echo '<h1>Instance OK</h1><p>Launched by Infra Tool.</p>' > /usr/share/nginx/html/index.html
  - systemctl enable nginx
  - systemctl start nginx
"""
        ni = [{
            "SubnetId": subnet_id,
            "DeviceIndex": 0,
            "AssociatePublicIpAddress": True,
            "Groups": [sg_id]
        }]
        params = dict(ImageId=ami_id, InstanceType=instance_type, MinCount=1, MaxCount=1, NetworkInterfaces=ni, UserData=user_data)
        if key_name: params["KeyName"] = key_name
        r = ec2.run_instances(**params)
        instance = r["Instances"][0]
        iid = instance["InstanceId"]
        ec2.create_tags(Resources=[iid], Tags=[{"Key": "Name", "Value": name_tag}])
        return jsonify({"ok": True, "instanceId": iid, "name": name_tag})
    except ClientError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# --- GitHub clone & preview ---
SAFE_REPO_RE = re.compile(r"^[a-zA-Z0-9_.\\-]+/[a-zA-Z0-9_.\\-]+$")

@app.post("/api/repo/clone")
def clone_repo():
    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    # Normalize URL (support "owner/repo")
    if "/" in url and not url.startswith("http"):
        if not SAFE_REPO_RE.match(url):
            return jsonify({"error": "Invalid repo format. Use owner/repo or full https URL"}), 400
        url = f"https://github.com/{url}.git"
    name = os.path.splitext(os.path.basename(url))[0] if url.endswith(".git") else os.path.basename(url)
    target = REPOS_BASE / name
    if target.exists():
        shutil.rmtree(target)
    try:
        if GIT_OK:
            Repo.clone_from(url, target, depth=1)
        else:
            subprocess.check_call(["git", "clone", "--depth", "1", url, str(target)])
        return jsonify({"ok": True, "name": name, "path": str(target), "previewUrl": f"/repos/{name}/"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.get("/repos/<name>/")
def serve_repo_index(name):
    repo_dir = (REPOS_BASE / name).resolve()
    if not repo_dir.exists() or REPOS_BASE not in repo_dir.parents:
        abort(404)
    index_path = repo_dir / "index.html"
    if index_path.exists():
        return send_from_directory(repo_dir, "index.html")
    files = [p.name for p in repo_dir.iterdir() if p.is_file()]
    items = "".join(f"<li><a href='/repos/{name}/{f}'>{f}</a></li>" for f in files)
    return Response(f"<h3>Fichiers du repo</h3><ul>{items}</ul>", mimetype="text/html")

@app.get("/repos/<name>/<path:filename>")
def serve_repo_file(name, filename):
    repo_dir = (REPOS_BASE / name).resolve()
    if not repo_dir.exists() or REPOS_BASE not in repo_dir.parents:
        abort(404)
    return send_from_directory(repo_dir, filename)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=True)
