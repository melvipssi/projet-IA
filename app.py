#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AWS Infra Helper - single file, UI améliorée, avec fallback DEMO si AWS n'est pas configuré.
- S3: lister / créer / uploader / supprimer
- EC2: lister / lancer (Nginx via user-data)
- GitHub: cloner (fallback "fake clone" si git indisponible) + prévisualiser
Lancer :
  pip install Flask boto3 GitPython
  python aws_infra_helper_single.py
"""

import os
import re
import json
import shutil
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, abort, Response, send_from_directory

# --- AWS (avec fallback demo) ---
try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError, EndpointConnectionError, NoRegionError
except Exception:
    boto3 = None
    ClientError = Exception
    NoCredentialsError = Exception
    EndpointConnectionError = Exception
    NoRegionError = Exception

# --- Git (optionnel) ---
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

# --------- AWS clients (si possible) ----------
session = None
s3 = ec2 = ssm = None
if boto3:
    try:
        session = boto3.Session(region_name=APP_REGION)
        s3 = session.client("s3")
        ec2 = session.client("ec2")
        ssm = session.client("ssm")
    except Exception:
        # on basculera en mode demo automatiquement dans les handlers
        pass

# --------- Helpers ---------
def _serialize_dt(dt):
    return dt.isoformat() if isinstance(dt, datetime) else dt

def _demo_mode_from_exc(e: Exception) -> bool:
    # Si les creds/région/endpoint manquent, on renvoie des données "demo" pour que l'UI fonctionne.
    demo_types = (NoCredentialsError, EndpointConnectionError, NoRegionError)
    return isinstance(e, demo_types) or "Unable to locate credentials" in str(e)

def _bucket_region(bucket):
    if not s3:
        return "eu-west-3"
    try:
        r = s3.get_bucket_location(Bucket=bucket)
        loc = r.get("LocationConstraint")
        return "us-east-1" if loc in (None, "") else loc
    except Exception:
        return "unknown"

def _empty_bucket(bucket):
    if not s3:
        return
    try:
        paginator = s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket):
            objs = [{"Key": v["Key"], "VersionId": v["VersionId"]} for v in page.get("Versions", [])]
            dms = [{"Key": m["Key"], "VersionId": m["VersionId"]} for m in page.get("DeleteMarkers", [])]
            to_del = objs + dms
            if to_del:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": to_del})
    except Exception:
        pass
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            objs = [{"Key": x["Key"]} for x in page.get("Contents", [])]
            if objs:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": objs})
    except Exception:
        pass

def _default_vpc_and_subnet():
    r_vpc = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])
    if not r_vpc["Vpcs"]:
        raise RuntimeError("No default VPC found")
    vpc_id = r_vpc["Vpcs"][0]["VpcId"]
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
    p = ssm.get_parameter(Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64")
    return p["Parameter"]["Value"]

# --------- UI (HTML/CSS/JS) ---------
INDEX_HTML = r"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AWS Infra Helper</title>
  <style>
    :root{--bg:#0b1020;--g1:#0f1733;--g2:#0a0f26;--tx:#e9edff;--mut:#a9b6ff;--bd:#27306b;--pri:#6f8cff;--ok:#1fbf75;--warn:#f1aa33;--err:#ef5a5a}
    *{box-sizing:border-box} body{margin:0;font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;background:radial-gradient(1400px 700px at -10% -10%, #1a2459 0%, #0b1130 40%, #0b1020 70%);color:var(--tx)}
    header{position:sticky;top:0;z-index:9;background:linear-gradient(135deg,var(--g1),var(--g2));border-bottom:1px solid rgba(255,255,255,.06);padding:18px 16px}
    .wrap{max-width:1200px;margin:0 auto}
    .brand{display:flex;gap:12px;align-items:center}
    .logo{width:34px;height:34px;border-radius:10px;background:linear-gradient(180deg,var(--pri),#3b56e6);display:grid;place-items:center;color:#fff;font-weight:700;box-shadow:0 8px 28px rgba(100,130,255,.25)}
    h1{font-size:20px;margin:0} .sub{color:var(--mut);font-size:13px;margin-top:4px}
    main{max-width:1200px;margin:0 auto;padding:18px 16px;display:grid;gap:16px}
    @media(min-width:900px){ main{grid-template-columns:1fr 1fr} .full{grid-column:1/-1} }
    .card{background:linear-gradient(180deg,#0f1733,#121a3c);border:1px solid var(--bd);border-radius:16px;padding:16px;box-shadow:0 10px 30px rgba(0,0,0,.25)}
    .card h2{margin:0 0 8px 0;font-size:18px}
    .desc{color:var(--mut);font-size:13px;margin-bottom:8px}
    .row{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
    input[type=text],input[type=file],input{background:#0b1335;border:1px solid #2a3770;color:var(--tx);border-radius:12px;padding:10px 12px;min-width:220px}
    input::placeholder{color:#97a2ff90}
    .btn{border:1px solid transparent;background:var(--pri);color:#fff;padding:10px 14px;border-radius:12px;cursor:pointer;font-weight:600}
    .btn.ghost{background:transparent;border-color:#2a3770;color:var(--tx)} .btn.danger{background:transparent;border-color:var(--err);color:#ffb8b8}
    .table{width:100%;border-collapse:collapse;margin-top:6px;font-size:14px}
    .table th,.table td{border-bottom:1px solid rgba(255,255,255,.06);padding:10px 8px;text-align:left}
    .badge{display:inline-block;padding:3px 9px;border-radius:999px;background:#1b2350;font-size:12px}
    .state.running{background:rgba(31,191,117,.15);color:#7df2bf;border:1px solid rgba(31,191,117,.25)}
    .state.stopped,.state.stopping,.state.shutting-down{background:rgba(241,170,51,.12);color:#ffd89a;border:1px solid rgba(241,170,51,.25)}
    .state.terminated{background:rgba(239,90,90,.15);color:#ffc3c3;border:1px solid rgba(239,90,90,.25)}
    #toasts{position:fixed;right:16px;top:16px;display:flex;flex-direction:column;gap:10px;z-index:9999}
    .toast{background:#101b42;border:1px solid #2a3770;border-radius:12px;padding:10px 12px;min-width:220px;box-shadow:0 10px 24px rgba(0,0,0,.25)}
    .toast.ok{border-color:rgba(31,191,117,.35)} .toast.err{border-color:rgba(239,90,90,.35)}
    .spinner{width:16px;height:16px;border:2px solid rgba(255,255,255,.25);border-top-color:#fff;border-radius:50%;display:inline-block;animation:spin .7s linear infinite;vertical-align:middle}
    @keyframes spin{to{transform:rotate(360deg)}} footer{opacity:.7;color:var(--mut);text-align:center;padding:18px 6px}
  </style>
</head>
<body>
  <header>
    <div class="wrap brand">
      <div class="logo">AI</div>
      <div><h1>AWS Infra Helper</h1><div class="sub">S3 • EC2 • GitHub — fonctionne en mode DEMO si AWS non configuré</div></div>
    </div>
  </header>

  <main>
    <section class="card">
      <h2>🪣 S3 — Buckets</h2>
      <div class="desc">Lister, créer, uploader, supprimer (vidage auto, versions incluses).</div>
      <div class="row"><button class="btn ghost" id="refreshBuckets">↻ Lister les buckets</button></div>
      <div class="row">
        <input id="newBucketName" placeholder="Nom du bucket (unique)">
        <input id="newBucketRegion" placeholder="Région (ex: eu-west-3)" value="eu-west-3">
        <button class="btn" id="createBucket">Créer</button>
      </div>
      <div class="row">
        <input id="uploadBucket" placeholder="Bucket pour upload">
        <input id="uploadPrefix" placeholder="Préfixe (optionnel dossier/)">
        <input type="file" id="uploadFile">
        <button class="btn" id="uploadBtn">Uploader</button>
      </div>
      <div id="buckets" class="desc">Clique sur “Lister les buckets”.</div>
    </section>

    <section class="card">
      <h2>🖥️ EC2 — Instances</h2>
      <div class="desc">Lister et lancer une instance publique (Nginx via user-data).</div>
      <div class="row"><button class="btn ghost" id="refreshInstances">↻ Lister les instances</button></div>
      <div class="row">
        <input id="ec2Name" placeholder="Tag Name" value="infra-tool-ec2">
        <input id="ec2Type" placeholder="Type (ex: t3.micro)" value="t3.micro">
        <input id="ec2Key" placeholder="Key pair (optionnel)">
        <input id="ec2Ami" placeholder="AMI (vide = AL2023 latest)">
        <button class="btn" id="launchEc2">Lancer une instance publique</button>
      </div>
      <div id="instances" class="desc">Clique sur “Lister les instances”.</div>
    </section>

    <section class="card full">
      <h2>🌐 GitHub — Cloner et prévisualiser</h2>
      <div class="desc">Saisis "owner/repo" ou une URL https; s’il y a un index.html, preview dispo.</div>
      <div class="row">
        <input id="repoUrl" placeholder="owner/repo ou URL https">
        <button class="btn" id="cloneRepo">Cloner</button>
      </div>
      <div id="repos" class="desc">Ex: vercel/next.js</div>
    </section>
  </main>

  <div id="toasts"></div>
  <footer>Flask + Boto3 • Démo pédagogique</footer>

<script>
  function el(tag, attrs={}, children=[]){
    const e = document.createElement(tag);
    for(const [k,v] of Object.entries(attrs)) e.setAttribute(k,v);
    (Array.isArray(children)?children:[children]).filter(Boolean).forEach(c=>e.appendChild(typeof c==='string'?document.createTextNode(c):c));
    return e;
  }
  function toast(msg, type='ok', timeout=3200){
    const box = document.getElementById('toasts');
    const t = el('div',{class:'toast '+type}, msg);
    box.appendChild(t);
    setTimeout(()=>{ t.style.opacity='0'; setTimeout(()=>t.remove(), 200); }, timeout);
  }
  async function api(path, options={}){
    const r = await fetch(path, options);
    let data={}; try{ data = await r.json(); }catch(e){}
    if(!r.ok) throw new Error(data.error || r.statusText || 'Erreur API');
    return data;
  }
  function setLoading(btn, loading){
    if(!btn) return;
    if(loading){ btn.dataset._txt = btn.textContent; btn.innerHTML = '<span class="spinner"></span>  Patiente...'; btn.setAttribute('disabled',''); }
    else { btn.innerHTML = btn.dataset._txt || 'OK'; btn.removeAttribute('disabled'); }
  }

  // S3
  const bucketsDiv = document.getElementById('buckets');
  document.getElementById('refreshBuckets').onclick = async ()=>{
    bucketsDiv.textContent = 'Chargement...';
    try{
      const b = await api('/api/s3');
      if(!b.length){ bucketsDiv.innerHTML = '<span>Aucun bucket.</span>'; return; }
      const table = el('table',{class:'table'});
      table.appendChild(el('thead',{},[el('tr',{},[el('th',{},'Nom'),el('th',{},'Région'),el('th',{},'Créé'),el('th',{},'Actions')])]));
      const tbody = el('tbody');
      b.forEach(x=>{
        const delBtn = el('button',{class:'btn danger'},'Supprimer');
        delBtn.onclick = async ()=>{
          if(!confirm('Supprimer '+x.name+' ? (il sera vidé)')) return;
          try{ setLoading(delBtn,true); await api('/api/s3/'+x.name,{method:'DELETE'}); toast('Bucket supprimé'); document.getElementById('refreshBuckets').click();}
          catch(e){ toast(e.message,'err'); } finally{ setLoading(delBtn,false); }
        };
        tbody.appendChild(el('tr',{},[ el('td',{},x.name), el('td',{},x.region), el('td',{},new Date(x.creationDate).toLocaleString()), el('td',{},delBtn)]));
      });
      table.appendChild(tbody);
      bucketsDiv.innerHTML=''; bucketsDiv.appendChild(table);
    }catch(e){ bucketsDiv.innerHTML = '<span>Erreur: '+e.message+'</span>'; }
  };

  document.getElementById('createBucket').onclick = async (ev)=>{
    const btn = ev.currentTarget;
    const name = document.getElementById('newBucketName').value.trim();
    const region = document.getElementById('newBucketRegion').value.trim() || 'eu-west-3';
    if(!name) return toast('Nom requis','err');
    try{ setLoading(btn,true); await api('/api/s3',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bucket_name:name,region})}); toast('Bucket créé'); document.getElementById('refreshBuckets').click();}
    catch(e){ toast(e.message,'err'); } finally{ setLoading(btn,false); }
  };

  document.getElementById('uploadBtn').onclick = async (ev)=>{
    const btn = ev.currentTarget;
    const bucket = document.getElementById('uploadBucket').value.trim();
    const prefix = document.getElementById('uploadPrefix').value.trim();
    const file = document.getElementById('uploadFile').files[0];
    if(!bucket || !file) return toast('Bucket et fichier requis','err');
    const fd = new FormData(); fd.append('bucket',bucket); fd.append('prefix',prefix); fd.append('file',file);
    try{ setLoading(btn,true); const res = await api('/api/s3/upload',{method:'POST',body:fd}); toast('Upload OK: '+res.key); }
    catch(e){ toast(e.message,'err'); } finally{ setLoading(btn,false); }
  };

  // EC2
  const instancesDiv = document.getElementById('instances');
  document.getElementById('refreshInstances').onclick = async ()=>{
    instancesDiv.textContent = 'Chargement...';
    try{
      const data = await api('/api/ec2');
      if(!data.length){ instancesDiv.innerHTML = '<span>Aucune instance.</span>'; return; }
      const table = el('table',{class:'table'});
      table.appendChild(el('thead',{},[el('tr',{},[el('th',{},'ID'),el('th',{},'Nom'),el('th',{},'Type'),el('th',{},'Etat'),el('th',{},'Public IP'),el('th',{},'Lancé')])]));
      const tbody = el('tbody');
      data.forEach(i=>{
        const chip = el('span',{class:'badge state '+String(i.state||'').toLowerCase()},i.state);
        tbody.appendChild(el('tr',{},[ el('td',{},i.instanceId), el('td',{},i.name||'-'), el('td',{},i.type), el('td',{},chip), el('td',{},i.publicIp||'-'), el('td',{},new Date(i.launchTime).toLocaleString()) ]));
      });
      table.appendChild(tbody);
      instancesDiv.innerHTML=''; instancesDiv.appendChild(table);
    }catch(e){ instancesDiv.innerHTML = '<span>Erreur: '+e.message+'</span>'; }
  };

  document.getElementById('launchEc2').onclick = async (ev)=>{
    const btn = ev.currentTarget;
    const body = { name: document.getElementById('ec2Name').value.trim(),
                   instance_type: document.getElementById('ec2Type').value.trim() || 't3.micro' };
    const key = document.getElementById('ec2Key').value.trim(); if(key) body.key_name = key;
    const ami = document.getElementById('ec2Ami').value.trim(); if(ami) body.ami_id = ami;
    try{ setLoading(btn,true); const res = await api('/api/ec2/launch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); toast('Instance lancée: '+res.instanceId); document.getElementById('refreshInstances').click(); }
    catch(e){ toast(e.message,'err'); } finally{ setLoading(btn,false); }
  };

  // GitHub
  const reposDiv = document.getElementById('repos');
  document.getElementById('cloneRepo').onclick = async (ev)=>{
    const btn = ev.currentTarget;
    const url = document.getElementById('repoUrl').value.trim();
    if(!url) return toast('URL ou owner/repo requis','err');
    try{
      setLoading(btn,true);
      const res = await api('/api/repo/clone',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})});
      const a = document.createElement('a'); a.href = res.previewUrl; a.target = '_blank'; a.textContent = res.previewUrl;
      reposDiv.innerHTML = ''; reposDiv.appendChild(document.createTextNode('Cloné: '+res.name+' — ')); reposDiv.appendChild(a);
      toast('Repo cloné');
    }catch(e){ toast(e.message,'err'); } finally{ setLoading(btn,false); }
  };
</script>
</body>
</html>
"""

# --------- Routes ---------
@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")

@app.get("/api/health")
def health():
    return jsonify(ok=True)

# ---- S3 ----
@app.get("/api/s3")
def list_buckets():
    # DEMO fallback
    if not s3:
        return jsonify([
            {"name": "demo-bucket-a", "creationDate": datetime(2024,1,1).isoformat(), "region": "eu-west-3"},
            {"name": "demo-bucket-b", "creationDate": datetime(2024,6,1).isoformat(), "region": "eu-west-1"},
        ])
    try:
        r = s3.list_buckets()
        out = []
        for b in r.get("Buckets", []):
            out.append({
                "name": b["Name"],
                "creationDate": _serialize_dt(b["CreationDate"]),
                "region": _bucket_region(b["Name"]),
            })
        return jsonify(out)
    except Exception as e:
        if _demo_mode_from_exc(e):
            return jsonify([
                {"name": "demo-bucket-a", "creationDate": datetime(2024,1,1).isoformat(), "region": "eu-west-3"},
                {"name": "demo-bucket-b", "creationDate": datetime(2024,6,1).isoformat(), "region": "eu-west-1"},
            ])
        return jsonify({"error": str(e)}), 400

@app.post("/api/s3")
def create_bucket():
    data = request.get_json(force=True, silent=True) or {}
    name = data.get("bucket_name")
    region = data.get("region") or APP_REGION
    if not name:
        return jsonify({"error": "bucket_name required"}), 400
    if not s3:
        # demo
        return jsonify({"ok": True, "bucket": name, "region": region, "demo": True})
    try:
        if region == "us-east-1":
            s3.create_bucket(Bucket=name)
        else:
            s3.create_bucket(Bucket=name, CreateBucketConfiguration={"LocationConstraint": region})
        return jsonify({"ok": True, "bucket": name, "region": region})
    except Exception as e:
        if _demo_mode_from_exc(e):
            return jsonify({"ok": True, "bucket": name, "region": region, "demo": True})
        return jsonify({"error": str(e)}), 400

@app.post("/api/s3/upload")
def upload_to_bucket():
    bucket = request.form.get("bucket")
    prefix = (request.form.get("prefix") or "").strip()
    f = request.files.get("file")
    if not bucket or not f:
        return jsonify({"error": "bucket and file are required"}), 400
    key = re.sub(r"[^\w\-. /]", "_", f.filename)
    if prefix:
        prefix = prefix.strip("/")
        key = f"{prefix}/{key}"
    if not s3:
        return jsonify({"ok": True, "bucket": bucket, "key": key, "url": f"https://demo/{bucket}/{key}", "demo": True})
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=f.stream.read())
        url = f"https://{bucket}.s3.amazonaws.com/{key}"
        return jsonify({"ok": True, "bucket": bucket, "key": key, "url": url})
    except Exception as e:
        if _demo_mode_from_exc(e):
            return jsonify({"ok": True, "bucket": bucket, "key": key, "url": f"https://demo/{bucket}/{key}", "demo": True})
        return jsonify({"error": str(e)}), 400

@app.delete("/api/s3/<bucket>")
def delete_bucket(bucket):
    if not s3:
        return jsonify({"ok": True, "bucket": bucket, "demo": True})
    try:
        _empty_bucket(bucket)
        s3.delete_bucket(Bucket=bucket)
        return jsonify({"ok": True, "bucket": bucket})
    except Exception as e:
        if _demo_mode_from_exc(e):
            return jsonify({"ok": True, "bucket": bucket, "demo": True})
        return jsonify({"error": str(e)}), 400

# ---- EC2 ----
@app.get("/api/ec2")
def list_instances():
    if not ec2:
        return jsonify([
            {"instanceId": "i-0DEMO1", "type": "t3.micro", "state": "running", "publicIp": "203.0.113.10", "privateIp": "10.0.0.5", "name": "demo-a", "launchTime": datetime(2024,7,1).isoformat(), "az": "eu-west-3a"},
            {"instanceId": "i-0DEMO2", "type": "t3.micro", "state": "stopped", "publicIp": None, "privateIp": "10.0.0.12", "name": "demo-b", "launchTime": datetime(2024,7,2).isoformat(), "az": "eu-west-3b"},
        ])
    try:
        out = []
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate():
            for r in page.get("Reservations", []):
                for i in r.get("Instances", []):
                    name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), None)
                    out.append({
                        "instanceId": i["InstanceId"],
                        "type": i.get("InstanceType"),
                        "state": i.get("State", {}).get("Name"),
                        "publicIp": i.get("PublicIpAddress"),
                        "privateIp": i.get("PrivateIpAddress"),
                        "name": name,
                        "launchTime": _serialize_dt(i.get("LaunchTime")),
                        "az": i.get("Placement", {}).get("AvailabilityZone"),
                    })
        return jsonify(out)
    except Exception as e:
        if _demo_mode_from_exc(e):
            return jsonify([
                {"instanceId": "i-0DEMO1", "type": "t3.micro", "state": "running", "publicIp": "203.0.113.10", "privateIp": "10.0.0.5", "name": "demo-a", "launchTime": datetime(2024,7,1).isoformat(), "az": "eu-west-3a"}
            ])
        return jsonify({"error": str(e)}), 400

@app.post("/api/ec2/launch")
def launch_instance():
    data = request.get_json(force=True, silent=True) or {}
    instance_type = data.get("instance_type", "t3.micro")
    key_name = data.get("key_name")
    ami_id = data.get("ami_id")
    name_tag = data.get("name", "infra-tool-ec2")

    if not ec2 or not ssm:
        # demo
        return jsonify({"ok": True, "instanceId": "i-0DEMO123", "name": name_tag, "demo": True})

    try:
        if not ami_id:
            ami_id = _latest_al2023_ami()
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
        if key_name:
            params["KeyName"] = key_name
        r = ec2.run_instances(**params)
        instance = r["Instances"][0]
        iid = instance["InstanceId"]
        ec2.create_tags(Resources=[iid], Tags=[{"Key": "Name", "Value": name_tag}])
        return jsonify({"ok": True, "instanceId": iid, "name": name_tag})
    except Exception as e:
        if _demo_mode_from_exc(e):
            return jsonify({"ok": True, "instanceId": "i-0DEMO123", "name": name_tag, "demo": True})
        return jsonify({"error": str(e)}), 400

# ---- GitHub ----
SAFE_REPO_RE = re.compile(r"^[a-zA-Z0-9_.\-]+/[a-zA-Z0-9_.\-]+$")

@app.post("/api/repo/clone")
def clone_repo():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url required"}), 400

    # owner/repo -> full URL
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
    except Exception:
        # fallback "fake clone" pour démo si git indisponible
        target.mkdir(parents=True, exist_ok=True)
        (target / "index.html").write_text("<h2>Demo preview</h2><p>Fake clone: repo not actually cloned.</p>", encoding="utf-8")
        return jsonify({"ok": True, "name": name, "path": str(target), "previewUrl": f"/repos/{name}/", "demo": True})

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

# --------- Run ---------
if __name__ == "__main__":
    # host 0.0.0.0 pour aussi tester sur IP locale si besoin
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=True)
