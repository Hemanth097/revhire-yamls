from fastapi import FastAPI, HTTPException, UploadFile, File, Depends
from pydantic import BaseModel
from celery import Celery

# Initialize FastAPI
app = FastAPI()

# Celery Configuration
celery = Celery(
    __name__,
    broker="redis://localhost:6379/0",  # Redis for broker
    backend="redis://localhost:6379/0"  # Redis for result backend
)

# Models
class CreateInstanceRequest(BaseModel):
    user_key: str 
    project_id: str = "outpost-443210"
    zone: str = "us-west1-a"
    instance_name: str = "vm-"
    machine_type: str = "e2-medium"
    image_project: str = "ubuntu-os-cloud"
    image_family: str = "ubuntu-2404-lts-amd64"


class Requriments(BaseModel):
    user_key: str
    project_id: str = "outpost-443210"
    zone: str = "us-west1-a"
    instance_name: str = "vm-"


@app.post("/upload-json/")
async def upload_json(user_key: str, file: UploadFile = File(...)):
    """
    Accepts a JSON file from the user, stores it in Vault under the user_key.
    """
    # Save the uploaded file temporarily
    file_location = f"/tmp/{file.filename}"
    with open(file_location, "wb") as f:
        f.write(await file.read())

    # Send the task to Celery to store the file content in Vault
    task = celery.send_task(
        "celery_worker.store_json_in_vault_task",
        args=[user_key, file_location]
    )

    # Return task ID to user
    return {"task_id": task.id, "status": "Upload and store task started"}


@app.post("/create-instance/")
def create_instance(request = Depends(CreateInstanceRequest)):
    """
    Sends a task to Celery to create a Compute Engine instance.
    """
    task = celery.send_task(
        "celery_worker.create_instance_task",
        args=[
            request.user_key,
            request.project_id,
            request.zone,
            request.instance_name,
            request.machine_type,
            request.image_project,
            request.image_family,
        ],
    )
    return {"task_id": task.id, "status": "Instance creation task started"}


@app.post("/install-k3s/")
def install_k3s(request = Depends(Requriments)):
    """
    Sends a task to Celery to install K3s on a Compute Engine instance.
    """
    task = celery.send_task(
        "celery_worker.install_k3s_task",
        args=[
            request.user_key,
            request.project_id,
            request.zone,
            request.instance_name,
        ],
    )
    return {"task_id": task.id, "status": "K3s installation task started"}



@app.post("/install-argocd/")
def install_argocd(request = Depends(Requriments)):
    """
    Sends a task to Celery to install ArgoCD on a Compute Engine instance.
    """
    task = celery.send_task(
        "celery_worker.install_argocd_task",
        args=[
            request.user_key,
            request.project_id,
            request.zone,
            request.instance_name,
        ],
    )
    return {"task_id": task.id, "status": "ArgoCD installation task started"}


@app.post("/install-infisical/")
def install_infisical(request = Depends(Requriments)):
    """
    Sends a task to Celery to install Infisical on a Compute Engine instance.
    """
    task = celery.send_task(
        "celery_worker.install_infisical_task",
        args=[
            request.user_key,
            request.project_id,
            request.zone,
            request.instance_name,
        ],
    )
    return {"task_id": task.id, "status": "Infisical installation task started"}

@app.post("/install-csi-drivers/")
def install_csi_driver(request = Depends(Requriments)):
    """
    Sends a task to Celery to install csi drivers on a Compute Engine instance.
    """
    task = celery.send_task(
        "celery_worker.install_csi_driver",
        args=[
            request.user_key,
            request.project_id,
            request.zone,
            request.instance_name,
        ],
    )
    return {"task_id": task.id, "status": "Infisical installation task started"}