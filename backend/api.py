"""API Routes for SRT Restreamer"""
from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status, WebSocket, WebSocketDisconnect
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import List, Optional
from models import InputStream, OutputStream, SessionLocal, get_db, init_db
from auth import (
    create_access_token, get_current_user, get_user_from_token,
    UserCreate, Token, create_default_user, get_password_hash,
    ACCESS_TOKEN_EXPIRE_MINUTES, authenticate_user
)
from stream_manager import stream_manager
from datetime import datetime, timedelta
import os
import json
import asyncio

router = APIRouter()

# ============ AUTH ============

@router.post("/auth/login", response_model=Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    from models import User
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(data={"sub": user.username}, expires_delta=access_token_expires)
    return {"access_token": access_token, "token_type": "bearer"}

def verify_password(plain, hashed):
    from auth import verify_password as vp
    return vp(plain, hashed)

@router.post("/auth/register")
def register(user: UserCreate, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    from models import User
    if db.query(User).filter(User.username == user.username).first():
        raise HTTPException(status_code=400, detail="Username already registered")
    db_user = User(username=user.username, hashed_password=get_password_hash(user.password))
    db.add(db_user)
    db.commit()
    return {"message": "User created"}


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=6)


@router.post("/auth/change-password")
def change_password(
    data: PasswordChange,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    from models import User
    if not verify_password(data.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if data.current_password == data.new_password:
        raise HTTPException(status_code=400, detail="New password must differ from the current password")
    current_user.hashed_password = get_password_hash(data.new_password)
    db.commit()
    return {"message": "Password updated successfully"}

# ============ INPUT STREAMS ============

class InputStreamCreate(BaseModel):
    name: str
    srt_url: str

class InputStreamUpdate(BaseModel):
    name: Optional[str] = None
    srt_url: Optional[str] = None

class InputStreamResponse(BaseModel):
    id: int
    name: str
    srt_url: str
    status: str
    status_message: str
    is_active: bool
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
        inp.status = status_info["status"]
        inp.status_message = status_info["message"]
        result.append({
            "id": inp.id,
            "name": inp.name,
            "srt_url": inp.srt_url,
            "status": inp.status,
            "status_message": inp.status_message,
            "is_active": inp.is_active,
            "thumbnail_path": inp.thumbnail_path,
            "created_at": inp.created_at.isoformat() if inp.created_at else "",
            "outputs_count": len(inp.outputs)
        })
    return result

@router.post("/inputs")
def create_input(data: InputStreamCreate, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    stream = InputStream(name=data.name, srt_url=data.srt_url)
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
        stream.srt_url = data.srt_url
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

    db.delete(stream)
    db.commit()
    return {"message": "Deleted"}

@router.post("/inputs/{stream_id}/start")
def start_input(stream_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    stream = db.query(InputStream).filter(InputStream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    if stream_manager.start_input(stream_id, stream.srt_url):
        stream.is_active = True
        stream.thumbnail_path = os.path.join(
            stream_manager.thumbnails_dir, f"input_{stream_id}.jpg"
        )
        db.commit()
        return {"message": "Input stream started"}
    else:
        raise HTTPException(status_code=500, detail="Failed to start stream")

@router.post("/inputs/{stream_id}/stop")
def stop_input(stream_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    stream = db.query(InputStream).filter(InputStream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    stream_manager.stop_input(stream_id)

    # Also stop all outputs
    for out in stream.outputs:
        stream_manager.stop_output(out.id)
        out.is_active = False

    stream.is_active = False
    stream.thumbnail_path = ""
    db.commit()
    return {"message": "Input stream stopped"}

def get_current_user_for_thumbnail(token: str = Query(None), db: Session = Depends(get_db)):
    """Allow thumbnails to be fetched via ?token= for <img> tags."""
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return get_user_from_token(token, db)

@router.get("/inputs/{stream_id}/thumbnail")
def get_thumbnail(stream_id: int, current_user = Depends(get_current_user_for_thumbnail)):
    path = os.path.join(stream_manager.thumbnails_dir, f"input_{stream_id}.jpg")
    if os.path.exists(path):
        from fastapi.responses import FileResponse
        return FileResponse(path, media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="Thumbnail not found")


@router.post("/inputs/{stream_id}/slate")
def upload_slate(stream_id: int, file: UploadFile = File(...), db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    stream = db.query(InputStream).filter(InputStream.id == stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Stream not found")

    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image files are allowed")

    os.makedirs(stream_manager.slates_dir, exist_ok=True)
    path = os.path.join(stream_manager.slates_dir, f"input_{stream_id}.jpg")
    try:
        contents = file.file.read()
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
    mode: str = "caller"  # caller or listener

class OutputStreamUpdate(BaseModel):
    name: Optional[str] = None
    srt_url: Optional[str] = None
    mode: Optional[str] = None  # caller or listener

class OutputStreamResponse(BaseModel):
    id: int
    input_stream_id: int
    name: str
    srt_url: str
    mode: str
    status: str
    status_message: str
    is_active: bool
    created_at: str

    class Config:
        from_attributes = True


class OutputConfig(BaseModel):
    name: str
    srt_url: str
    mode: str = "caller"


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

@router.get("/outputs/{input_id}", response_model=List[OutputStreamResponse])
def get_outputs(input_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    outputs = db.query(OutputStream).filter(OutputStream.input_stream_id == input_id).all()
    result = []
    for out in outputs:
        status_info = stream_manager.get_output_status(out.id)
        out.status = status_info["status"]
        out.status_message = status_info["message"]
        result.append({
            "id": out.id,
            "input_stream_id": out.input_stream_id,
            "name": out.name,
            "srt_url": out.srt_url,
            "mode": out.mode,
            "status": out.status,
            "status_message": out.status_message,
            "is_active": out.is_active,
            "created_at": out.created_at.isoformat() if out.created_at else ""
        })
    return result

@router.post("/outputs")
def create_output(data: OutputStreamCreate, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    stream = db.query(InputStream).filter(InputStream.id == data.input_stream_id).first()
    if not stream:
        raise HTTPException(status_code=404, detail="Input stream not found")

    out = OutputStream(
        input_stream_id=data.input_stream_id,
        name=data.name,
        srt_url=data.srt_url,
        mode=data.mode
    )
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
        out.srt_url = data.srt_url
    if data.mode:
        out.mode = data.mode
    db.commit()
    return {"message": "Updated"}

@router.delete("/outputs/{output_id}")
def delete_output(output_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    out = db.query(OutputStream).filter(OutputStream.id == output_id).first()
    if not out:
        raise HTTPException(status_code=404, detail="Output not found")

    stream_manager.stop_output(output_id)
    db.delete(out)
    db.commit()
    return {"message": "Deleted"}

@router.post("/outputs/{output_id}/start")
def start_output(output_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    out = db.query(OutputStream).filter(OutputStream.id == output_id).first()
    if not out:
        raise HTTPException(status_code=404, detail="Output not found")

    stream = db.query(InputStream).filter(InputStream.id == out.input_stream_id).first()
    if not stream or not stream.is_active:
        raise HTTPException(status_code=400, detail="Input stream is not active")

    if stream_manager.start_output(stream.id, output_id, out.srt_url):
        out.is_active = True
        db.commit()
        return {"message": "Output stream started"}
    else:
        raise HTTPException(status_code=500, detail="Failed to start output")

@router.post("/outputs/{output_id}/stop")
def stop_output(output_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    out = db.query(OutputStream).filter(OutputStream.id == output_id).first()
    if not out:
        raise HTTPException(status_code=404, detail="Output not found")

    stream_manager.stop_output(output_id)
    out.is_active = False
    db.commit()
    return {"message": "Output stopped"}

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
            "outputs": out_stats
        })
    return stats

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    # Validate JWT token before accepting WebSocket connection
    db = SessionLocal()
    try:
        get_current_user(token=token, db=db)
    except Exception:
        await websocket.close(code=1008)
        return
    finally:
        db.close()

    await websocket.accept()
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
                        "outputs": out_stats
                    })
                await websocket.send_json({"type": "stats", "data": data})
            finally:
                db.close()
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        pass


# ============ IMPORT / EXPORT ============

@router.get("/export")
def export_config(db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    """Download all inputs and their outputs as a JSON configuration file."""
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
    mode: str = Query("append", regex="^(append|replace)$"),
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
        payload = json.loads(raw)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
    finally:
        file.file.close()

    try:
        config = ConfigImport(**payload)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid config format: {e}")

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
        inp = InputStream(name=item.name, srt_url=item.srt_url)
        db.add(inp)
        db.flush()
        created_inputs += 1
        new_inputs.append(inp)

        for out_cfg in item.outputs:
            out = OutputStream(
                input_stream_id=inp.id,
                name=out_cfg.name,
                srt_url=out_cfg.srt_url,
                mode=out_cfg.mode
            )
            db.add(out)
            created_outputs += 1

    db.commit()

    if start:
        for inp in new_inputs:
            if stream_manager.start_input(inp.id, inp.srt_url):
                inp.is_active = True
                inp.thumbnail_path = os.path.join(
                    stream_manager.thumbnails_dir, f"input_{inp.id}.jpg"
                )
                started_inputs += 1

                for out in inp.outputs:
                    if stream_manager.start_output(inp.id, out.id, out.srt_url):
                        out.is_active = True
                        started_outputs += 1
        db.commit()

    return {
        "message": "Configuration imported successfully",
        "mode": mode,
        "created_inputs": created_inputs,
        "created_outputs": created_outputs,
        "started_inputs": started_inputs,
        "started_outputs": started_outputs
    }
