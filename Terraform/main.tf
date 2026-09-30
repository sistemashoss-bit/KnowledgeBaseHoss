terraform {
  required_providers {
    local = {
      source  = "hashicorp/local"
      version = ">= 2.5.2"
    }
  }
}

provider "google" {
  project = "carbide-tenure-428618-v7"
  region  = "us-central1"
  zone    = "us-central1-c"
}

resource "google_compute_firewall" "allow_web" {
  name    = "allow-hosscomunicacion-web"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }

  source_ranges = ["0.0.0.0/0"]

  target_tags = ["hosscomunicacion-web"]
}

resource "google_compute_firewall" "allow_ssh" {
  name    = "allow-hosscomunicacion-ssh"
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = ["0.0.0.0/0"]

  target_tags = ["hosscomunicacion-web"]
}


# IP estática: sin esto la VM usa una IP efímera que cambia al detenerla o
# redimensionarla, y hay que actualizar el DNS en cPanel. `address` reserva la
# IP que ya tiene la VM (GCP permite promover una efímera en uso a estática).
resource "google_compute_address" "static_ip" {
  name    = "hosscomunicacion-ip"
  region  = "us-central1"
  address = "136.65.155.81"
}

resource "google_compute_instance" "vm_instance" {
  name                      = "hosscomunicacion"
  machine_type              = "e2-small"
  tags                      = ["hosscomunicacion-web"]
  allow_stopping_for_update = true

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
      size  = 30
      type  = "pd-standard"
    }
  }

  network_interface {
    network = "default"
    access_config {
      nat_ip = google_compute_address.static_ip.address
    }
  }
  metadata = {
    ssh-keys = "hoss:ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGASx1fiay8mBvEUlIDyDOHBF4Ed5ZT9LsXT5EYzxrCP"
  }
}



# Obtain Server IP for ansible
output "server_ip" {
  value = google_compute_instance.vm_instance.network_interface.0.access_config.0.nat_ip
}

# Create Ansible inventory.ini for running
resource "local_file" "deploy_inventory" {
  content = <<EOF
[servers]
hosscomunicacion ansible_host=${google_compute_instance.vm_instance.network_interface.0.access_config.0.nat_ip} ansible_user=hoss
EOF

  filename = "../Ansible/inventory.ini"
}