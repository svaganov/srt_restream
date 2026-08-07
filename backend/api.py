"""API Routes for SRT Restreamer"""
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import List, Optional
from models import InputStream, OutputStream, SessionLocal, get_db
from auth import (
    verify_password,
    hash_password,
    get_current_user,
    get_current_user_ws,
    create_session,
    logout_session,
    revoke_all_user_sessions,
    check_origin,
    check_login_rate_limit,
    SESSION_LIFETIME_MINUTES,
    MIN_PASSWORD_LENGTH,
)
from stream_manager import stream_manager
from srt_url import SrtUrl
from encryption import encrypt, decrypt
from events import event_bus
from datetime import datetime
import os
import json
import asyncio

router = APIRouter()


def _emit_user(user, level: str, category: str, event: str, message: str,
               stream_id=None, stream_name=None):
    """Emit an event caused by a UI action, attributed to the operator."""
    event_bus.emit(
        level, category, event, message,
        stream_id=stream_id, stream_name=stream_name,
        source=f"user:{user.username}",
    )


def _session_cookie_attributes(request: Request) -> dict:
    """Return secure session cookie attributes."""
    secure = os.getenv("SESSION_COOKIE_SECURE", "true").lower() != "false"
    return {
        "key": "session",
        "httponly": True,
        "secure": secure,
        "samesite": "strict",
        "max_age": SESSION_LIFETIME_MINUTES * 60,
        "path": "/",
    }


# ============ AUTH ============

@router.post("/auth/login")
def login(
    request: Request,
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    from models import User
    check_origin(request)
    client_ip = request.client.host if request.client else "unknown"
    check_login_rate_limit(client_ip)
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    token, csrf_token = create_session(db, user.id)
    attrs = _session_cookie_attributes(request)
    response.set_cookie(value=token, **attrs)
    # Double-submit CSRF cookie: JS reads it and echoes it in X-CSRF-Token.
    csrf_attrs = {**attrs, "key": "csrf_token", "httponly": False}
    response.set_cookie(value=csrf_token, **csrf_attrs)
    return {"csrf_token": csrf_token}


@router.get("/auth/me")
def auth_me(current_user = Depends(get_current_user)):
    """Session probe used by the SPA instead of a client-readable token."""
    return {"username": current_user.username}


@router.post("/auth/logout")
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    token = request.cookies.get("session")
    logout_session(db, token)
    attrs = _session_cookie_attributes(request)
    response.delete_cookie(**{k: v for k, v in attrs.items() if k in ("key", "path", "samesite", "secure", "httponly")})
    response.delete_cookie(key="csrf_token", path="/", samesite=attrs["samesite"], secure=attrs["secure"])
    return {"message": "Logged out"}


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=MIN_PASSWORD_LENGTH)


@router.post("/auth/change-password")
def change_password(
    request: Request,
    response: Response,
    data: PasswordChange,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    if not verify_password(data.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if data.current_password == data.new_password:
        raise HTTPException(status_code=400, detail="New password must differ from the current password")
    current_user.hashed_password = hash_password(data.new_password)
    # Invalidate all sessions; user must log in again.
    revoke_all_user_sessions(db, current_user.id)
    db.commit()
    attrs = _session_cookie_attributes(request)
    response.delete_cookie(**{k: v for k, v in attrs.items() if k in ("key", "path", "samesite", "secure", "httponly")})
    response.delete_cookie(key="csrf_token", path="/", samesite=attrs["samesite"], secure=attrs["secure"])
    return {"message": "Password updated. Please log in again."}


# ============ SRT URL helpers ============

def _apply_srt_url(
    stream_obj,
    url: str,
    passphrase: Optional[str] = None,
    explicit_mode: Optional[str] = None,
):
    """Validate an SRT URL and apply it together with an optional passphrase."""
    try:
        srt = SrtUrl.parse(url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid SRT URL: {exc}")

    if explicit_mode is not None and explicit_mode.lower() != srt.mode:
        raise HTTPException(
            status_code=422,
            detail=f"Explicit mode '{explicit_mode}' conflicts with URL mode '{srt.mode}'",
        )

    stream_obj.srt_url = url
    stream_obj.mode = srt.mode
    if passphrase:
        stream_obj.passphrase_encrypted = encrypt(passphrase)
    else:
        stream_obj.passphrase_encrypted = ""


def _has_passphrase(stream_obj) -> bool:
    return bool(stream_obj.passphrase_encrypted)


def _input_response_dict(inp, status_info: dict) -> dict:
    return {
        "id": inp.id,
        "name": inp.name,
        "srt_url": inp.srt_url,
        "mode": inp.mode,
        "status": status_info["status"],
        "status_message": status_info["message"],
        "is_active": inp.is_active,
        "desired_state": "active" if inp.desired_active else "stopped",
        "runtime_state": status_info["status"],
        "has_passphrase": _has_passphrase(inp),
        "thumbnail_path": inp.thumbnail_path,
        "created_at": inp.created_at.isoformat() if inp.created_at else "",
        "outputs_count": len(inp.outputs),
    }


def _output_response_dict(out, status_info: dict) -> dict:
    return {
        "id": out.id,
        "input_stream_id": out.input_stream_id,
        "name": out.name,
        "srt_url": out.srt_url,
        "mode": out.mode,
        "status": status_info["status"],
        "status_message": status_info["message"],
        "is_active": out.is_active,
        "desired_state": "active" if out.desired_active else "stopped",
        "runtime_state": status_info["status"],
        "has_passphrase": _has_passphrase(out),
        "created_at": out.created_at.isoformat() if out.created_at else "",
    }


# ============ INPUT STREAMS ============

class InputStreamCreate(BaseModel):
    name: str
    srt_url: str
    passphrase: Optional[str] = None


class InputStreamUpdate(BaseModel):
    name: Optional[str] = None
    srt_url: Optional[str] = None
    passphrase: Optional[str] = None


class InputStreamResponse(BaseModel):
    id: int
    name: str
    srt_url: str
    mode: str
    status: str
    status_message: str
    is_active: bool
    desired_state: str
    runtime_state: str
    has_passphrase: bool
    thumbnail_path: str
    created_at: str
    outputs_count: int = 0

    class Config:
        from_attributes = True


@router.get("/inputs", response_model=List[InputStreamResponse])
def get_inputs(db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    inputs = db.query(InputStream).all()
    result = []
    for inp in inputs:
        status_info = stream_manager.get_input_status(inp.id)
        result.append(_input_response_dict(inp, status_info))
    return result


@router.post("/inputs")
def create_input(data: InputStreamCreate, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    stream = InputStream(name=data.name)
    _apply_srt_url(stream, data.srt_url, passphrase=data.passphrase)
    db.add(stream)
    db.commit()
    db.refresh(stream)
    return {"id": stream.id, "message": "Input stream created"}


@router.put("/inputs/{stream_id}")
def update_input(stream_id: int, data: InputStreamUpdate, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    stream = db.query(InputStream).filter(InputStream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    # Cannot edit while running
    if stream.is_active:
        raise HTTPException(status_code=400, detail="Stop the input stream before editing")

    if data.name:
        stream.name = data.name
    if data.srt_url:
        _apply_srt_url(stream, data.srt_url, passphrase=data.passphrase)
    elif data.passphrase is not None:
        # Only passphrase changed
        if data.passphrase:
            stream.passphrase_encrypted = encrypt(data.passphrase)
        else:
            stream.passphrase_encrypted = ""
    db.commit()
    return {"message": "Updated"}


@router.delete("/inputs/{stream_id}")
def delete_input(stream_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    stream = db.query(InputStream).filter(InputStream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    # Stop all processes
    stream_manager.stop_input(stream_id)
    for out in stream.outputs:
        stream_manager.stop_output(out.id)

    _emit_user(current_user, "warning", "input", "stream_deleted",
               "Input deleted with all outputs", stream_id=stream_id, stream_name=stream.name)
    db.delete(stream)
    db.commit()
    return {"message": "Deleted"}


@router.post("/inputs/{stream_id}/start", status_code=202)
def start_input(stream_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    """Idempotent: marks the input as desired and (re)binds internal sockets.

    The actual runtime state is reported via REST/WebSocket status.
    """
    stream = db.query(InputStream).filter(InputStream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    if stream_manager.start_input(stream_id, stream.srt_url, passphrase_encrypted=stream.passphrase_encrypted or None, name=stream.name):
        stream.is_active = True
        stream.desired_active = True
        stream.thumbnail_path = os.path.join(
            stream_manager.thumbnails_dir, f"input_{stream_id}.jpg"
        )
        db.commit()
        _emit_user(current_user, "info", "input", "stream_started",
                   "Input start requested", stream_id=stream_id, stream_name=stream.name)
        return {"desired_state": "active", "message": "Input stream start requested"}
    else:
        _emit_user(current_user, "error", "input", "stream_failed",
                   "Failed to start input", stream_id=stream_id, stream_name=stream.name)
        raise HTTPException(status_code=500, detail="Failed to start stream")


@router.post("/inputs/{stream_id}/stop", status_code=202)
def stop_input(stream_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    stream = db.query(InputStream).filter(InputStream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    stream_manager.stop_input(stream_id)

    # Also stop all outputs
    for out in stream.outputs:
        stream_manager.stop_output(out.id)
        out.is_active = False
        out.desired_active = False

    stream.is_active = False
    stream.desired_active = False
    stream.thumbnail_path = ""
    db.commit()
    _emit_user(current_user, "info", "input", "stream_stopped",
               "Input stop requested", stream_id=stream_id, stream_name=stream.name)
    return {"desired_state": "stopped", "message": "Input stream stop requested"}


@router.get("/inputs/{stream_id}/thumbnail")
def get_thumbnail(stream_id: int, current_user = Depends(get_current_user)):
    path = os.path.join(stream_manager.thumbnails_dir, f"input_{stream_id}.jpg")
    if os.path.exists(path):
        from fastapi.responses import FileResponse
        return FileResponse(path, media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="Thumbnail not found")


MAX_SLATE_SIZE_BYTES = 10 * 1024 * 1024


@router.post("/inputs/{stream_id}/slate")
def upload_slate(stream_id: int, file: UploadFile = File(...), db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    stream = db.query(InputStream).filter(InputStream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files are allowed")

    contents = file.file.read()
    if len(contents) > MAX_SLATE_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="Slate image exceeds 10 MiB limit")

    os.makedirs(stream_manager.slates_dir, exist_ok=True)
    path = os.path.join(stream_manager.slates_dir, f"input_{stream_id}.jpg")
    try:
        with open(path, "wb") as f:
            f.write(contents)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save slate: {e}")
    finally:
        file.file.close()

    return {"message": "Slate image updated"}


@router.delete("/inputs/{stream_id}/slate")
def delete_slate(stream_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    stream = db.query(InputStream).filter(InputStream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    path = os.path.join(stream_manager.slates_dir, f"input_{stream_id}.jpg")
    if os.path.exists(path):
        os.remove(path)
    return {"message": "Slate image removed, using default NO SIGNAL"}


# ============ OUTPUT STREAMS ============

class OutputStreamCreate(BaseModel):
    input_stream_id: int
    name: str
    srt_url: str
    mode: Optional[str] = None  # deprecated, derived from URL
    passphrase: Optional[str] = None


class OutputStreamUpdate(BaseModel):
    name: Optional[str] = None
    srt_url: Optional[str] = None
    mode: Optional[str] = None  # deprecated, derived from URL
    passphrase: Optional[str] = None


class OutputStreamResponse(BaseModel):
    id: int
    input_stream_id: int
    name: str
    srt_url: str
    mode: str
    status: str
    status_message: str
    is_active: bool
    desired_state: str
    runtime_state: str
    has_passphrase: bool
    created_at: str

    class Config:
        from_attributes = True


@router.get("/outputs/{input_id}", response_model=List[OutputStreamResponse])
def get_outputs(input_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    outputs = db.query(OutputStream).filter(OutputStream.input_stream_id == input_id).all()
    result = []
    for out in outputs:
        status_info = stream_manager.get_output_status(out.id)
        result.append(_output_response_dict(out, status_info))
    return result


@router.post("/outputs")
def create_output(data: OutputStreamCreate, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    stream = db.query(InputStream).filter(InputStream.id == data.input_stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Input stream not found")

    out = OutputStream(input_stream_id=data.input_stream_id, name=data.name)
    _apply_srt_url(out, data.srt_url, passphrase=data.passphrase, explicit_mode=data.mode)
    db.add(out)
    db.commit()
    db.refresh(out)
    return {"id": out.id, "message": "Output stream created"}


@router.put("/outputs/{output_id}")
def update_output(output_id: int, data: OutputStreamUpdate, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    out = db.query(OutputStream).filter(OutputStream.id == output_id).first()
    if not out:
        raise HTTPException(status_code=404, detail="Output not found")

    # Cannot edit while running
    if out.is_active:
        raise HTTPException(status_code=400, detail="Stop the output before editing")

    if data.name:
        out.name = data.name
    if data.srt_url:
        _apply_srt_url(out, data.srt_url, passphrase=data.passphrase, explicit_mode=data.mode)
    elif data.passphrase is not None:
        if data.passphrase:
            out.passphrase_encrypted = encrypt(data.passphrase)
        else:
            out.passphrase_encrypted = ""
    db.commit()
    return {"message": "Updated"}


@router.delete("/outputs/{output_id}")
def delete_output(output_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    out = db.query(OutputStream).filter(OutputStream.id == output_id).first()
    if not out:
        raise HTTPException(status_code=404, detail="Output not found")

    stream_manager.stop_output(output_id)
    _emit_user(current_user, "warning", "output", "output_deleted",
               "Output deleted", stream_id=output_id, stream_name=out.name)
    db.delete(out)
    db.commit()
    return {"message": "Deleted"}


@router.post("/outputs/{output_id}/start", status_code=202)
def start_output(output_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    out = db.query(OutputStream).filter(OutputStream.id == output_id).first()
    if not out:
        raise HTTPException(status_code=404, detail="Output not found")

    stream = db.query(InputStream).filter(InputStream.id == out.input_stream_id).first()
    if not stream or not stream.is_active:
        raise HTTPException(status_code=400, detail="Input stream is not active")

    if stream_manager.start_output(stream.id, output_id, out.srt_url, passphrase_encrypted=out.passphrase_encrypted or None, name=out.name):
        out.is_active = True
        out.desired_active = True
        db.commit()
        _emit_user(current_user, "info", "output", "output_started",
                   "Output start requested", stream_id=output_id, stream_name=out.name)
        return {"desired_state": "active", "message": "Output stream start requested"}
    else:
        _emit_user(current_user, "error", "output", "stream_failed",
                   "Failed to start output", stream_id=output_id, stream_name=out.name)
        raise HTTPException(status_code=500, detail="Failed to start output")


@router.post("/outputs/{output_id}/stop", status_code=202)
def stop_output(output_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    out = db.query(OutputStream).filter(OutputStream.id == output_id).first()
    if not out:
        raise HTTPException(status_code=404, detail="Output not found")

    stream_manager.stop_output(output_id)
    out.is_active = False
    out.desired_active = False
    db.commit()
    _emit_user(current_user, "info", "output", "output_stopped",
               "Output stop requested", stream_id=output_id, stream_name=out.name)
    return {"desired_state": "stopped", "message": "Output stop requested"}


# ============ SYSTEM ACTIONS ============

@router.post("/system/restart-streams", status_code=202)
def restart_streams(current_user = Depends(get_current_user)):
    """Stop every runtime process; the supervisor respawns desired streams."""
    result = stream_manager.restart_all()
    _emit_user(current_user, "warning", "system", "restart_all",
               f"Restart all: stopped {result['stopped_inputs']} inputs, {result['stopped_outputs']} outputs")
    return {
        "message": "All streams stopped; desired streams are restarting",
        **result,
    }


@router.post("/system/kill-orphans", status_code=200)
def kill_orphans(current_user = Depends(get_current_user)):
    """Terminate orphaned FFmpeg processes left by previous app generations."""
    result = stream_manager.kill_orphans()
    killed = result.get("killed", [])
    _emit_user(
        current_user, "warning", "system", "orphans_killed",
        f"Killed {len(killed)} orphan FFmpeg processes" + (f": {killed}" if killed else ""),
    )
    return result


@router.get("/events")
def get_events(limit: int = Query(200, ge=1, le=1000), current_user = Depends(get_current_user)):
    """Most recent events for the UI events panel."""
    return event_bus.list(limit=limit)


# ============ SRT STATISTICS ============

@router.get("/inputs/{stream_id}/srt-stats")
def get_input_srt_stats(stream_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    """Get detailed SRT statistics for an input (requires active SRT proxy)."""
    stream = db.query(InputStream).filter(InputStream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")
    return stream_manager.get_input_srt_stats(stream_id)


# ============ STATS & WS ============

@router.get("/stats")
def get_stats(db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    inputs = db.query(InputStream).all()
    stats = []
    for inp in inputs:
        inp_status = stream_manager.get_input_status(inp.id)
        out_stats = []
        for out in inp.outputs:
            out_status = stream_manager.get_output_status(out.id)
            out_stats.append({
                "id": out.id,
                "name": out.name,
                "status": out_status["status"],
                "message": out_status["message"],
                "stats": out_status["stats"]
            })

        stats.append({
            "input_id": inp.id,
            "input_name": inp.name,
            "input_status": inp_status["status"],
            "input_message": inp_status["message"],
            "input_stats": inp_status["stats"],
            "input_srt_stats": stream_manager.get_input_srt_stats(inp.id),
            "outputs": out_stats
        })
    return stats


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # Validate session cookie and origin before accepting WebSocket connection
    from models import User

    try:
        check_origin(websocket)
    except HTTPException:
        await websocket.close(code=1008)
        return

    db = SessionLocal()
    try:
        get_current_user_ws(websocket, db)
    except HTTPException:
        await websocket.close(code=1008)
        return
    finally:
        db.close()

    await websocket.accept()

    # Events are queued by the bus (thread-safe) and drained on each stats tick.
    import queue as _queue
    event_queue: "_queue.Queue" = _queue.Queue()
    event_bus.subscribe(event_queue.put_nowait)

    try:
        while True:
            # Send stats every 2 seconds
            db = SessionLocal()
            try:
                inputs = db.query(InputStream).all()
                data = []
                for inp in inputs:
                    inp_status = stream_manager.get_input_status(inp.id)
                    out_stats = []
                    for out in inp.outputs:
                        out_status = stream_manager.get_output_status(out.id)
                        out_stats.append({
                            "id": out.id,
                            "name": out.name,
                            "status": out_status["status"],
                            "message": out_status["message"],
                            "stats": out_status["stats"]
                        })
                    data.append({
                        "input_id": inp.id,
                        "input_status": inp_status["status"],
                        "input_message": inp_status["message"],
                        "input_stats": inp_status["stats"],
                        "input_srt_stats": stream_manager.get_input_srt_stats(inp.id),
                        "outputs": out_stats
                    })
                await websocket.send_json({"type": "stats", "data": data})
            finally:
                db.close()

            # Forward any queued events to the client.
            while True:
                try:
                    item = event_queue.get_nowait()
                except _queue.Empty:
                    break
                await websocket.send_json({"type": "event", "data": item})

            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        pass
    finally:
        event_bus.unsubscribe(event_queue.put_nowait)


# ============ IMPORT / EXPORT ============

class OutputConfig(BaseModel):
    name: str
    srt_url: str
    mode: Optional[str] = None


class InputConfig(BaseModel):
    name: str
    srt_url: str
    outputs: List[OutputConfig] = []


class ConfigExport(BaseModel):
    version: int = 1
    exported_at: str
    inputs: List[InputConfig]


class ConfigImport(BaseModel):
    version: int = 1
    exported_at: Optional[str] = None
    inputs: List[InputConfig]


MAX_IMPORT_SIZE_BYTES = 1 * 1024 * 1024
MAX_IMPORT_INPUTS = 100
MAX_IMPORT_OUTPUTS = 500


@router.get("/export")
def export_config(db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    """Download all inputs and their outputs as a JSON configuration file.

    Passphrases are intentionally not exported and must be re-entered after import.
    """
    inputs = db.query(InputStream).order_by(InputStream.id).all()
    data = {
        "version": 1,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "inputs": []
    }
    for inp in inputs:
        data["inputs"].append({
            "name": inp.name,
            "srt_url": inp.srt_url,
            "outputs": [
                {
                    "name": out.name,
                    "srt_url": out.srt_url,
                    "mode": out.mode
                }
                for out in inp.outputs
            ]
        })

    content = json.dumps(data, indent=2, ensure_ascii=False)
    return Response(
        content=content,
        media_type="application/json",
        headers={
            "Content-Disposition": "attachment; filename=restreamer-config.json"
        }
    )


@router.post("/import")
def import_config(
    file: UploadFile = File(...),
    mode: str = Query("append", pattern="^(append|replace)$"),
    start: bool = Query(False),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Upload a JSON configuration file to create inputs and outputs.

    mode=append  - add new inputs/outputs to existing ones (default)
    mode=replace - delete existing inputs/outputs and replace them with the file
    start=true   - automatically start all imported inputs and outputs
    """
    try:
        raw = file.file.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to read upload: {e}")
    finally:
        file.file.close()

    if len(raw) > MAX_IMPORT_SIZE_BYTES:
        raise HTTPException(status_code=413, detail="Import file exceeds 1 MiB limit")

    try:
        payload = json.loads(raw)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    try:
        config = ConfigImport(**payload)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid config format: {e}")

    if len(config.inputs) > MAX_IMPORT_INPUTS:
        raise HTTPException(status_code=413, detail=f"Import exceeds {MAX_IMPORT_INPUTS} inputs")

    total_outputs = sum(len(item.outputs) for item in config.inputs)
    if total_outputs > MAX_IMPORT_OUTPUTS:
        raise HTTPException(status_code=413, detail=f"Import exceeds {MAX_IMPORT_OUTPUTS} outputs")

    # Validate all SRT URLs before touching the database.
    try:
        for item in config.inputs:
            SrtUrl.parse(item.srt_url)
            for out_cfg in item.outputs:
                parsed = SrtUrl.parse(out_cfg.srt_url)
                if out_cfg.mode is not None and parsed.mode != out_cfg.mode:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Output '{out_cfg.name}': mode '{out_cfg.mode}' conflicts with URL mode '{parsed.mode}'",
                    )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=f"Invalid SRT URL in import file: {e}")

    if mode == "replace":
        # Stop all running streams before removing them
        existing = db.query(InputStream).all()
        for inp in existing:
            stream_manager.stop_input(inp.id)
            for out in inp.outputs:
                stream_manager.stop_output(out.id)
        db.query(OutputStream).delete()
        db.query(InputStream).delete()
        db.commit()
        # Clean up stale slate images
        try:
            for fname in os.listdir(stream_manager.slates_dir):
                fpath = os.path.join(stream_manager.slates_dir, fname)
                if os.path.isfile(fpath):
                    os.remove(fpath)
        except Exception:
            pass

    created_inputs = 0
    created_outputs = 0
    started_inputs = 0
    started_outputs = 0
    new_inputs = []

    for item in config.inputs:
        parsed = SrtUrl.parse(item.srt_url)
        inp = InputStream(name=item.name, srt_url=item.srt_url, mode=parsed.mode)
        db.add(inp)
        db.flush()
        created_inputs += 1
        new_inputs.append(inp)

        for out_cfg in item.outputs:
            out_parsed = SrtUrl.parse(out_cfg.srt_url)
            out = OutputStream(
                input_stream_id=inp.id,
                name=out_cfg.name,
                srt_url=out_cfg.srt_url,
                mode=out_parsed.mode,
            )
            db.add(out)
            created_outputs += 1

    db.commit()

    if start:
        for inp in new_inputs:
            if stream_manager.start_input(inp.id, inp.srt_url, passphrase_encrypted=inp.passphrase_encrypted or None):
                inp.is_active = True
                inp.desired_active = True
                inp.thumbnail_path = os.path.join(
                    stream_manager.thumbnails_dir, f"input_{inp.id}.jpg"
                )
                started_inputs += 1

                for out in inp.outputs:
                    if stream_manager.start_output(inp.id, out.id, out.srt_url, passphrase_encrypted=out.passphrase_encrypted or None):
                        out.is_active = True
                        out.desired_active = True
                        started_outputs += 1
        db.commit()

    _emit_user(
        current_user, "info", "system", "imported",
        f"Config imported ({mode}): {created_inputs} inputs, {created_outputs} outputs",
    )
    return {
        "message": "Configuration imported successfully",
        "mode": mode,
        "created_inputs": created_inputs,
        "created_outputs": created_outputs,
        "started_inputs": started_inputs,
        "started_outputs": started_outputs
    }
