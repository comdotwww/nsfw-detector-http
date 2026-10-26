"""Token 管理端（独立端口）。

单独跑一个 uvicorn 进程（默认 6087 端口，只监听 localhost），
与 v1 推理服务完全隔离。包含：

- 登录页：输入 ADMIN_TOKEN（docker 环境变量注入，替代原 TOKEN）
- 管理页：token 列表（名称/有效期/每日上限/总量上限/用量/启用状态）+
  创建/编辑/删除/重置用量/显示明文
- JSON API（同样要登录态）：
    GET    /api/admin/tokens
    POST   /api/admin/tokens
    PUT    /api/admin/tokens/{id}
    DELETE /api/admin/tokens/{id}
    POST   /api/admin/tokens/{id}/reset

该服务共享 data/tokens.db（v1 校验服务也读同一份），WAL 模式下多进程安全。
"""

from __future__ import annotations

import hashlib
import logging
import os
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import tokens
from app.utils.helper import show_json

log = logging.getLogger("nsfw.admin")

# 登录密钥（环境变量）；必须设置，否则管理端口无法登录
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# 登录态 cookie 名称与有效期（12 小时）
_CRUMB_NAME = "nsfw_admin"
_CRUMB_TTL = 12 * 3600
_login_sessions: dict[str, float] = {}  # crumb -> expire_ts


def _cookie_value() -> str:
    return hashlib.sha256(ADMIN_TOKEN.encode("utf-8")).hexdigest()


def _authorized(request: Request) -> bool:
    """管理 API 鉴权：要么带 X-Admin-Token header，要么带有效的 cookie。"""
    if not ADMIN_TOKEN:
        return False
    if request.headers.get("X-Admin-Token") == ADMIN_TOKEN:
        return True
    crumb = request.cookies.get(_CRUMB_NAME)
    if crumb and crumb == _cookie_value() and _login_sessions.get(crumb, 0) > time.time():
        return True
    return False


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>NSFW Token 管理 - 登录</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;background:#101418;color:#e6e8eb;
     display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{background:#1a1f26;border:1px solid #2b323b;border-radius:12px;padding:32px;width:340px}
h1{font-size:18px;margin:0 0 4px} p.sub{color:#9aa4b0;font-size:13px;margin:0 0 20px}
input{width:100%;box-sizing:border-box;padding:10px 12px;border-radius:8px;border:1px solid #333;
      background:#101418;color:#e6e8eb;font-size:14px;margin-bottom:12px}
button{width:100%;padding:10px;border:0;border-radius:8px;background:#3b82f6;color:#fff;
       font-size:14px;font-weight:600;cursor:pointer}
button:hover{background:#2f6fd6}
.err{color:#f87171;font-size:13px;margin-top:10px;display:block}
</style></head><body>
<div class="card">
  <h1>NSFW Token 管理</h1>
  <p class="sub">请输入管理密钥（ADMIN_TOKEN）</p>
  <form method="post" action="/login">
    <input type="password" name="token" placeholder="管理密钥" autofocus required>
    <button type="submit">登录</button>
    <span class="err" id="err"></span>
  </form>
</div>
<script>
const url=new URLSearchParams(location.search);
if(url.get('e')==='1') document.getElementById('err').textContent='密钥错误';
</script>
</body></html>"""

_MANAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>NSFW Token 管理</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;background:#101418;color:#e6e8eb;margin:0;padding:24px}
h1{font-size:20px;margin:0 0 16px}
.logout{float:right;font-size:13px;color:#9aa4b0;text-decoration:none}
table{width:100%;border-collapse:collapse;background:#1a1f26;border-radius:12px;overflow:hidden;margin-bottom:24px}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #2b323b;font-size:13px;vertical-align:top}
th{background:#20262e;font-weight:600;color:#c3ccd6}
td .muted{color:#7d8894}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:12px;margin-right:6px}
.badge.on{background:#16331f;color:#4ade80}.badge.off{background:#3a1d1d;color:#f87171}
.badge.limit{background:#1e293b;color:#94a3b8}
.btn{padding:5px 12px;border-radius:6px;border:1px solid #333;background:#20262e;color:#cbd5e1;
     font-size:12px;cursor:pointer;text-decoration:none;display:inline-block;margin:2px}
.btn:hover{background:#2b333c}
.btn.danger{color:#f87171;border-color:#5b2a2a}.btn.danger:hover{background:#3a1d1d}
.btn.primary{background:#3b82f6;border-color:#3b82f6;color:#fff}
.raw{background:#101418;border:1px solid #2b323b;border-radius:8px;padding:8px 12px;
     font-family:ui-monospace,monospace;font-size:12px;color:#a5f3fc;word-break:break-all}
.formgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px;margin-bottom:12px}
input,select{width:100%;box-sizing:border-box;padding:8px 10px;border-radius:8px;border:1px solid #333;
      background:#101418;color:#e6e8eb;font-size:13px}
.card{background:#1a1f26;border:1px solid #2b323b;border-radius:12px;padding:20px;margin-bottom:24px}
.card h2{margin:0 0 14px;font-size:15px}
.toast{position:fixed;top:20px;right:20px;background:#2b333c;color:#e6e8eb;padding:10px 16px;
       border-radius:8px;font-size:13px;opacity:0;transition:opacity .2s;z-index:99}
.toast.show{opacity:1}
@media(max-width:700px){table{display:block;overflow-x:auto}}
</style></head><body>
<a class="logout" href="/logout">登出</a>
<h1>NSFW Token 管理</h1>

<div class="card">
  <h2>新建 Token</h2>
  <form id="createForm" onsubmit="createToken(event)">
    <div class="formgrid">
      <input name="name" placeholder="名称（必填，如：客户A）" required>
      <input name="expires" type="date" placeholder="有效期（空=永久）">
      <input name="daily_limit" type="number" min="0" placeholder="每日上限（0=不限）" value="0">
      <input name="total_limit" type="number" min="0" placeholder="总量上限（0=不限）" value="0">
    </div>
    <button class="btn primary" type="submit">创建</button>
  </form>
</div>

<div class="card">
  <h2>Token 列表</h2>
  <table>
    <thead><tr>
      <th>ID</th><th>名称</th><th>Token</th><th>有效期</th><th>限次</th><th>用量</th><th>状态</th><th>操作</th>
    </tr></thead>
    <tbody id="tbody"><tr><td colspan="8" class="muted">加载中…</td></tr></tbody>
  </table>
</div>

<div id="rawCard" class="card" style="display:none">
  <h2>新 Token（仅显示一次，请立即保存）</h2>
  <div class="raw" id="rawToken"></div>
</div>

<div class="toast" id="toast"></div>

<script>
let LISTED=[];
async function api(url,opt){
  const r=await fetch(url,{headers:{'Content-Type':'application/json'},...opt});
  const j=await r.json().catch(()=>({}));
  if(!r.ok || j.code!==200){throw new Error(j.msg||('HTTP '+r.status));}
  return j.data;
}
function toast(msg){const t=document.getElementById('toast');t.textContent=msg;t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),2500);}
function fmtLimit(t){const parts=[];
  if(t.daily_limit>0)parts.push('日 '+t.daily_limit);else parts.push('日 ∞');
  if(t.total_limit>0)parts.push('总 '+t.total_limit);else parts.push('总 ∞');
  return parts.join(' / ');}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function refresh(){
  try{ LISTED=await api('/api/admin/tokens'); }catch(e){ toast('读取失败: '+e.message); return; }
  const tb=document.getElementById('tbody');
  if(!LISTED.length){tb.innerHTML='<tr><td colspan="8" class="muted">还没有 Token，先在上方创建一个</td></tr>';return;}
  tb.innerHTML=LISTED.map(t=>`<tr>
    <td>${t.id}</td>
    <td>${esc(t.name)}</td>
    <td class="muted"><code>${esc(t.token_prefix)}…</code></td>
    <td>${esc(t.expires_text)}</td>
    <td class="muted">${fmtLimit(t)}</td>
    <td><b>${t.total_used}</b><span class="muted"> / ${t.daily_used} 今日</span></td>
    <td>${t.enabled
        ?'<span class="badge on">启用</span>'
        :'<span class="badge off">停用</span>'}</td>
    <td>
      <button class="btn" onclick="toggleEnabled(${t.id})">${t.enabled?'停用':'启用'}</button>
      <button class="btn" onclick="resetUsage(${t.id})">重置用量</button>
      <button class="btn danger" onclick="delToken(${t.id})">删除</button>
    </td></tr>`).join('');
}
async function createToken(ev){
  ev.preventDefault();
  const f=ev.target;
  const expiresEl=f.querySelector('[name=expires]');
  let expiresAt=0;
  if(expiresEl.value){expiresAt=Math.floor(new Date(expiresEl.value+'T23:59:59')/1000);}
  try{
    const t=await api('/api/admin/tokens',{method:'POST',body:JSON.stringify({
      name:f.querySelector('[name=name]').value.trim(),
      expires_at:expiresAt,
      daily_limit:Number(f.querySelector('[name=daily_limit]').value||0),
      total_limit:Number(f.querySelector('[name=total_limit]').value||0),
    })});
    document.getElementById('rawToken').textContent=t.raw_token;
    document.getElementById('rawCard').style.display='block';
    f.reset();
    toast('已创建');
    refresh();
  }catch(e){toast('创建失败: '+e.message);}
}
async function toggleEnabled(id){
  const t=LISTED.find(x=>x.id===id); if(!t)return;
  try{ await api('/api/admin/tokens/'+id,{method:'PUT',body:JSON.stringify({enabled:!t.enabled})});
       toast('已'+(t.enabled?'停用':'启用')); refresh(); }
  catch(e){toast('操作失败: '+e.message);}
}
async function resetUsage(id){
  try{ await api('/api/admin/tokens/'+id+'/reset',{method:'POST'}); toast('已重置'); refresh(); }
  catch(e){toast('重置失败: '+e.message);}
}
async function delToken(id){
  if(!confirm('确定删除 ID='+id+' 的 Token？删除后不可恢复。'))return;
  try{ await api('/api/admin/tokens/'+id,{method:'DELETE'}); toast('已删除'); refresh(); }
  catch(e){toast('删除失败: '+e.message);}
}
refresh();
</script></body></html>"""


# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------

admin_app = FastAPI(title="NSFW Token 管理", docs_url=None, redoc_url=None, openapi_url=None)


@admin_app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if _authorized(request):
        return HTMLResponse(_MANAGE_HTML)
    return HTMLResponse(_LOGIN_HTML)


@admin_app.post("/login")
async def login(request: Request):
    form = await request.form()
    token = str(form.get("token", ""))
    # 常量时间比较
    if not ADMIN_TOKEN or not _tok_eq(token, ADMIN_TOKEN):
        return RedirectResponse("/?e=1", status_code=303)
    crumb = _cookie_value()
    _login_sessions[crumb] = time.time() + _CRUMB_TTL
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(_CRUMB_NAME, crumb, max_age=_CRUMB_TTL, httponly=True,
                    samesite="lax")
    return resp


@admin_app.get("/logout")
async def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(_CRUMB_NAME)
    return resp


def _tok_eq(a: str, b: str) -> bool:
    import hmac
    return hmac.compare_digest(a, b)


def _require_auth(request: Request):
    if not _authorized(request):
        raise HTTPException(status_code=401, detail="unauthorized")


@admin_app.get("/api/admin/tokens")
async def list_tokens_api(request: Request):
    _require_auth(request)
    return show_json(200, "success", await tokens.list_tokens())


@admin_app.post("/api/admin/tokens")
async def create_token_api(request: Request):
    _require_auth(request)
    body = await request.json()
    name = str(body.get("name", "")).strip()
    if not name:
        return show_json(-1000, "名称不能为空")
    created = await tokens.create(
        name=name,
        expires_at=int(body.get("expires_at", 0) or 0),
        daily_limit=max(0, int(body.get("daily_limit", 0) or 0)),
        total_limit=max(0, int(body.get("total_limit", 0) or 0)),
    )
    if not created:
        return show_json(-1000, "创建失败")
    return show_json(200, "success", created)


@admin_app.put("/api/admin/tokens/{token_id}")
async def update_token_api(token_id: int, request: Request):
    _require_auth(request)
    body = await request.json()
    kw = {}
    if "name" in body:
        kw["name"] = str(body["name"]).strip()
    if "expires_at" in body:
        kw["expires_at"] = int(body["expires_at"] or 0)
    if "daily_limit" in body:
        kw["daily_limit"] = max(0, int(body["daily_limit"] or 0))
    if "total_limit" in body:
        kw["total_limit"] = max(0, int(body["total_limit"] or 0))
    if "enabled" in body:
        kw["enabled"] = bool(body["enabled"])
    updated = await tokens.update(token_id, **kw)
    if not updated:
        return show_json(-1000, "Token 不存在或更新失败")
    return show_json(200, "success", updated)


@admin_app.delete("/api/admin/tokens/{token_id}")
async def delete_token_api(token_id: int, request: Request):
    _require_auth(request)
    ok = await tokens.delete(token_id)
    if not ok:
        return show_json(-1000, "Token 不存在")
    return show_json(200, "success", {"id": token_id})


@admin_app.post("/api/admin/tokens/{token_id}/reset")
async def reset_token_api(token_id: int, request: Request):
    _require_auth(request)
    ok = await tokens.reset_usage(token_id)
    if not ok:
        return show_json(-1000, "Token 不存在")
    return show_json(200, "success", {"id": token_id})


@admin_app.get("/api/admin/stats")
async def admin_stats(request: Request):
    _require_auth(request)
    return show_json(200, "success", await tokens.stats())