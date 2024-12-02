from celery import Celery
import hvac
import json
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import os
import subprocess

# Celery Configuration
celery = Celery(
    __name__,
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/0"
)

# Vault Configuration
VAULT_URL = "http://127.0.0.1:8200"
VAULT_TOKEN = "hvs.1k2QVFmqC2LSIgKA9QklnW7R"
VAULT_SECRET_PATH = "gcp"
client = hvac.Client(url=VAULT_URL, token=VAULT_TOKEN)

def fetch_credentials_from_vault(user_key):
    """
    Fetch credentials for the specified user_key from Vault.
    """
    secret_path = f"{VAULT_SECRET_PATH}/{user_key}"
    secret = client.secrets.kv.v2.read_secret_version(path=secret_path)
    return secret["data"]["data"]


@celery.task
def store_json_in_vault_task(user_key, file_path):
    """
    Reads the uploaded JSON file and stores it in Vault under the specified user_key.
    """
    try:
        # Read the JSON file
        with open(file_path, "r") as file:
            json_data = json.load(file)
        
        # Store the JSON in Vault under the user_key
        secret_path = f"{VAULT_SECRET_PATH}/{user_key}"
        client.secrets.kv.v2.create_or_update_secret(path=secret_path, secret=json_data)

        # Clean up the temporary file
        os.remove(file_path)

        return {"status": "JSON successfully stored in Vault", "user_key": user_key}
    except Exception as e:
        return {"status": "Failed to store JSON in Vault", "error": str(e)}

@celery.task
def create_instance_task(user_key, project_id, zone, instance_name, machine_type, image_project, image_family):
    """
    Creates a Compute Engine instance with an SSH key for authentication and opens port 22 for SSH access.
    """
    # Fetch credentials from Vault
    credentials = fetch_credentials_from_vault(user_key)

    # Create a temporary JSON for GCP authentication
    temp_json_path = "/tmp/temp_gcp_creds.json"
    with open(temp_json_path, "w") as temp_file:
        json.dump(credentials, temp_file)

    # Authenticate using Google SDK
    gcp_credentials = Credentials.from_service_account_file(temp_json_path)
    service = build("compute", "v1", credentials=gcp_credentials)

    # Read the SSH public key
    ssh_key_path = os.path.expanduser("~/.ssh/my_gcp_key.pub")
    if not os.path.exists(ssh_key_path):
        raise FileNotFoundError(f"SSH public key not found at {ssh_key_path}. Please generate the key.")

    with open(ssh_key_path, "r") as ssh_file:
        ssh_key = ssh_file.read().strip()

    # Add the public key to instance metadata
    ssh_metadata = f"your-ssh-username:{ssh_key}"

    # Define the instance configuration
    instance_body = {
        "name": instance_name,
        "machineType": f"zones/{zone}/machineTypes/{machine_type}",
        "disks": [
            {
                "boot": True,
                "autoDelete": True,
                "initializeParams": {
                    "sourceImage": f"projects/{image_project}/global/images/family/{image_family}"
                }
            }
        ],
        "networkInterfaces": [
            {
                "network": "global/networks/default",
                "accessConfigs": [{"type": "ONE_TO_ONE_NAT"}]  # Assign external IP to the instance
            }
        ],
        "metadata": {
            "items": [{"key": "ssh-keys", "value": ssh_metadata}]
        }
    }

    # Create the instance and configure firewall rules
    try:
        # Step 1: Open port 22 for SSH access
        firewall_body = {
            "name": f"allow-ssh-{instance_name}",
            "allowed": [{"IPProtocol": "tcp", "ports": ["22"]}],
            "direction": "INGRESS",
            "priority": 1000,
            "sourceRanges": ["0.0.0.0/0"],
            "targetTags": [instance_name]
        }

        firewall_request = service.firewalls().insert(project=project_id, body=firewall_body)
        firewall_request.execute()

        # Step 2: Create the instance
        instance_body["tags"] = {"items": [instance_name]}  # Tag for firewall rule targeting
        request = service.instances().insert(
            project=project_id,
            zone=zone,
            body=instance_body
        )
        response = request.execute()

        # Clean up the temporary JSON
        os.remove(temp_json_path)

        return {"status": "Instance created successfully", "response": response}
    except Exception as e:
        os.remove(temp_json_path)
        return {"status": "Error", "message": str(e)}


@celery.task
def install_k3s_task(user_key, project_id, zone, instance_name):
    """
    Installs K3s and K9s on a Compute Engine instance.
    """
    # Fetch credentials from Vault
    credentials = fetch_credentials_from_vault(user_key)

    # Create a temporary JSON for GCP authentication
    temp_json_path = "/tmp/temp_gcp_creds.json"
    with open(temp_json_path, "w") as temp_file:
        json.dump(credentials, temp_file)

    # Authenticate using Google SDK
    gcp_credentials = Credentials.from_service_account_file(temp_json_path)
    service = build("compute", "v1", credentials=gcp_credentials)

    # Fetch the instance's external IP
    try:
        instance = service.instances().get(project=project_id, zone=zone, instance=instance_name).execute()
        external_ip = instance["networkInterfaces"][0]["accessConfigs"][0]["natIP"]
    except Exception as e:
        return {"status": "Error", "message": str(e)}

    # Path to the private key
    private_key_path = os.path.expanduser("~/.ssh/my_gcp_key")
    if not os.path.exists(private_key_path):
        raise FileNotFoundError(f"SSH private key not found at {private_key_path}. Please ensure the key exists.")

    # SSH into the instance and install K3s
    try:
        # Install K3s
        ssh_command_k3s = [
            "ssh", "-i", private_key_path,
            "-o", "StrictHostKeyChecking=no",
            f"your-ssh-username@{external_ip}",
            "curl -sfL https://get.k3s.io | sh -"
        ]
        subprocess.run(ssh_command_k3s, check=True)

        # Install K9s after K3s installation with sudo
        install_k9s_command = [
            "ssh", "-i", private_key_path,
            "-o", "StrictHostKeyChecking=no",
            f"your-ssh-username@{external_ip}",
            """
            # Create a temporary directory for K9s installation
            TEMP_DIR=$(mktemp -d)
            cd $TEMP_DIR

            # Download K9s tarball
            curl -sS -L https://github.com/derailed/k9s/releases/download/v0.27.1/k9s_Linux_amd64.tar.gz -o k9s.tar.gz

            # Check if the download was successful
            if [ $? -ne 0 ]; then
                echo "Error downloading K9s."
                exit 1
            fi

            # Extract the tarball with sudo to /usr/local/bin
            sudo tar -xvzf k9s.tar.gz -C /usr/local/bin

            # Clean up the temporary directory
            rm -rf $TEMP_DIR
            """
        ]
        subprocess.run(install_k9s_command, check=True)

    except Exception as e:
        return {"status": "Error", "message": f"Failed to install K3s and K9s: {str(e)}"}

    # Clean up the temporary JSON
    os.remove(temp_json_path)

    return {"status": "K3s and K9s installed successfully on instance", "instance": instance_name}


@celery.task
def install_argocd_task(user_key, project_id, zone, instance_name):
    """
    Installs ArgoCD on a Compute Engine instance.
    """
    # Fetch credentials from Vault (or wherever you store them)
    credentials = fetch_credentials_from_vault(user_key)

    # Create a temporary JSON for GCP authentication
    temp_json_path = "/tmp/temp_gcp_creds.json"
    with open(temp_json_path, "w") as temp_file:
        json.dump(credentials, temp_file)

    # Authenticate using Google SDK
    gcp_credentials = Credentials.from_service_account_file(temp_json_path)
    service = build("compute", "v1", credentials=gcp_credentials)

    # Fetch the instance's external IP
    try:
        instance = service.instances().get(project=project_id, zone=zone, instance=instance_name).execute()
        external_ip = instance["networkInterfaces"][0]["accessConfigs"][0]["natIP"]
    except Exception as e:
        return {"status": "Error", "message": str(e)}

    # Path to the private key
    private_key_path = os.path.expanduser("~/.ssh/my_gcp_key")
    if not os.path.exists(private_key_path):
        raise FileNotFoundError(f"SSH private key not found at {private_key_path}. Please ensure the key exists.")

    # SSH into the instance and install ArgoCD
    try:
        
        # Step 1: Fix permissions for k3s.yaml
        fix_kube_config_permissions_command = [
            "ssh", "-i", private_key_path,
            "-o", "StrictHostKeyChecking=no",
            f"your-ssh-username@{external_ip}",
            """
            sudo chmod 644 /etc/rancher/k3s/k3s.yaml
            sudo chown $(id -u):$(id -g) /etc/rancher/k3s/k3s.yaml
            """
        ]
        subprocess.run(fix_kube_config_permissions_command, check=True)


        # # Step 2: Install kubectl if not already installed
        # install_kubectl_command = [
        #     "ssh", "-i", private_key_path,
        #     "-o", "StrictHostKeyChecking=no",
        #     f"your-ssh-username@{external_ip}",
        #     """
        #     # Add the Kubernetes APT repository
        #     curl -s https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo apt-key add -
        #     echo "deb https://apt.kubernetes.io/ kubernetes-xenial main" | sudo tee -a /etc/apt/sources.list.d/kubernetes.list
        #     sudo apt-get update

        #     # Install kubectl
        #     sudo apt-get install -y kubectl
        #     """
        # ]
        # subprocess.run(install_kubectl_command, check=True)


        # Step 3: Install ArgoCD using kubectl
        install_argocd_command = [
            "ssh", "-i", private_key_path,
            "-o", "StrictHostKeyChecking=no",
            f"your-ssh-username@{external_ip}",
            """
            kubectl create namespace argocd
            kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml
            """
        ]
        subprocess.run(install_argocd_command, check=True)

        # Step 4: Expose ArgoCD via LoadBalancer
        expose_argocd_command = [
            "ssh", "-i", private_key_path,
            "-o", "StrictHostKeyChecking=no",
            f"your-ssh-username@{external_ip}",
            """
            kubectl patch svc argocd-server -n argocd -p '{"spec": {"type": "NodePort", "ports": [{"port": 80, "targetPort": 8080, "nodePort": 32123}]}}'
            """
        ]
        subprocess.run(expose_argocd_command, check=True)

    except Exception as e:
        return {"status": "Error", "message": f"Failed to install ArgoCD: {str(e)}"}

    # Clean up the temporary JSON
    os.remove(temp_json_path)

    return {"status": "ArgoCD installed successfully and exposed in nodeport 32123", "instance": instance_name}

@celery.task
def install_infisical_task(user_key, project_id, zone, instance_name):
    """
    Installs Helm and Infisical on a Compute Engine instance.
    """
    # Fetch credentials from Vault (or wherever you store them)
    credentials = fetch_credentials_from_vault(user_key)

    # Create a temporary JSON for GCP authentication
    temp_json_path = "/tmp/temp_gcp_creds.json"
    with open(temp_json_path, "w") as temp_file:
        json.dump(credentials, temp_file)

    # Authenticate using Google SDK
    gcp_credentials = Credentials.from_service_account_file(temp_json_path)
    service = build("compute", "v1", credentials=gcp_credentials)

    # Fetch the instance's external IP
    try:
        instance = service.instances().get(project=project_id, zone=zone, instance=instance_name).execute()
        external_ip = instance["networkInterfaces"][0]["accessConfigs"][0]["natIP"]
    except Exception as e:
        return {"status": "Error", "message": str(e)}

    # Path to the private key
    private_key_path = os.path.expanduser("~/.ssh/my_gcp_key")
    if not os.path.exists(private_key_path):
        raise FileNotFoundError(f"SSH private key not found at {private_key_path}. Please ensure the key exists.")

    # SSH into the instance and perform operations
    try:
        # Step 1: Install Helm
        install_helm_command = [
            "ssh", "-i", private_key_path,
            "-o", "StrictHostKeyChecking=no",
            f"your-ssh-username@{external_ip}",
            """
            curl -fsSL -o get_helm.sh https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3
            chmod 700 get_helm.sh
            ./get_helm.sh
            """
        ]
        subprocess.run(install_helm_command, check=True)

        # Step 2: Add the Helm repo for Infisical
        add_helm_repo_command = [
            "ssh", "-i", private_key_path,
            "-o", "StrictHostKeyChecking=no",
            f"your-ssh-username@{external_ip}",
            """
            helm repo add infisical-helm-charts 'https://dl.cloudsmith.io/public/infisical/helm-charts/helm/charts/'
            helm repo update
            """
        ]
        subprocess.run(add_helm_repo_command, check=True)

        # Ensure kubectl can access K3s
        configure_kubectl_command = [
            "ssh", "-i", private_key_path,
            "-o", "StrictHostKeyChecking=no",
            f"your-ssh-username@{external_ip}",
            """
            export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
            sudo chmod 644 /etc/rancher/k3s/k3s.yaml
            sudo chown $(id -u):$(id -g) /etc/rancher/k3s/k3s.yaml
            """
        ]
        subprocess.run(configure_kubectl_command, check=True)


        # Step 3: Install Infisical using Helm
        install_infisical_command = [
            "ssh", "-i", private_key_path,
            "-o", "StrictHostKeyChecking=no",
            f"your-ssh-username@{external_ip}",
            """
            export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
            kubectl create namespace infisical || true
            helm install infisical infisical-helm-charts/infisical -n infisical
            """
        ]
        subprocess.run(install_infisical_command, check=True)



        # Step 4: Expose Infisical via NodePort
        expose_infisical_command = [
            "ssh", "-i", private_key_path,
            "-o", "StrictHostKeyChecking=no",
            f"your-ssh-username@{external_ip}",
            """
            export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
            kubectl patch svc infisical-backend -n infisical -p '{"spec": {"type": "NodePort", "ports": [{"port": 8080, "targetPort": 8080, "nodePort": 32323}]}}'
            
            """
        ]
        subprocess.run(expose_infisical_command, check=True)



    except Exception as e:
        return {"status": "Error", "message": f"Failed to install Infisical: {str(e)}"}

    # Clean up the temporary JSON
    os.remove(temp_json_path)

    return {"status": "Infisical installed successfully and exposed on NodePort 32323", "instance": instance_name}

@celery.task
def install_csi_driver(user_key, project_id, zone, instance_name):
    """
    Installs the GCP CSI driver on a Compute Engine instance using kubectl.
    """
    # Fetch credentials from Vault (or wherever you store them)
    credentials = fetch_credentials_from_vault(user_key)

    # Create a temporary JSON for GCP authentication
    temp_json_path = "/tmp/temp_gcp_creds.json"
    with open(temp_json_path, "w") as temp_file:
        json.dump(credentials, temp_file)

    # Authenticate using Google SDK
    gcp_credentials = Credentials.from_service_account_file(temp_json_path)
    service = build("compute", "v1", credentials=gcp_credentials)

    # Fetch the instance's external IP
    try:
        instance = service.instances().get(project=project_id, zone=zone, instance=instance_name).execute()
        external_ip = instance["networkInterfaces"][0]["accessConfigs"][0]["natIP"]
    except Exception as e:
        return {"status": "Error", "message": str(e)}

    # Path to the private key
    private_key_path = os.path.expanduser("~/.ssh/my_gcp_key")
    if not os.path.exists(private_key_path):
        raise FileNotFoundError(f"SSH private key not found at {private_key_path}. Please ensure the key exists.")

    try:
        # Clone the CSI driver repository on the instance
        clone_repo_command = [
            "ssh", "-i", private_key_path,
            "-o", "StrictHostKeyChecking=no",
            f"your-ssh-username@{external_ip}",
            """
            kubectl create namespace gce-pd-csi-driver 
            git clone https://github.com/kubernetes-sigs/gcp-compute-persistent-disk-csi-driver.git /tmp/gcp-csi-driver
            """
        ]
        subprocess.run(clone_repo_command, check=True)

        # Apply the YAML manifests for the stable master overlay
        apply_csi_manifests_command = [
            "ssh", "-i", private_key_path,
            "-o", "StrictHostKeyChecking=no",
            f"your-ssh-username@{external_ip}",
            """
            export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
            cd /tmp/gcp-csi-driver/deploy/kubernetes/overlays/stable-master
            kubectl apply -k .
            """
        ]
        subprocess.run(apply_csi_manifests_command, check=True)

    except Exception as e:
        return {"status": "Error", "message": f"Failed to install CSI driver: {str(e)}"}

    # Clean up the temporary JSON
    os.remove(temp_json_path)

    return {"status": "GCP CSI driver installed successfully", "instance": instance_name}