#!/bin/bash
# Setup script for the GPU instance (g6.xlarge / g5.xlarge)
# Run on the GPU instance after launch:
#   curl -sL https://raw.githubusercontent.com/dharm0809/Provn/feature/data-integrity-engine/scripts/setup_gpu_instance.sh | bash

set -e

echo "=== Installing NVIDIA drivers + Ollama ==="

# Install NVIDIA drivers (Amazon Linux 2023)
sudo dnf install -y kernel-modules-extra 2>/dev/null || true
sudo dnf install -y nvidia-driver 2>/dev/null || {
    echo "Installing NVIDIA from CUDA repo..."
    sudo dnf config-manager --add-repo https://developer.download.nvidia.com/compute/cuda/repos/amzn2023/x86_64/cuda-amzn2023.repo 2>/dev/null || true
    sudo dnf install -y nvidia-driver-latest 2>/dev/null || true
}

echo ""
echo "=== Installing Ollama ==="
curl -fsSL https://ollama.ai/install.sh | sh

echo ""
echo "=== Starting Ollama ==="
# Configure Ollama to listen on all interfaces (so gateway instance can reach it)
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf > /dev/null << 'EOF'
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
EOF

sudo systemctl daemon-reload
sudo systemctl enable ollama
sudo systemctl start ollama
sleep 5

echo ""
echo "=== Pulling models ==="
ollama pull llama3.1:8b
ollama pull mistral:7b

echo ""
echo "=== Verify GPU ==="
nvidia-smi 2>/dev/null || echo "nvidia-smi not available yet (may need reboot)"
ollama list

echo ""
echo "=== Quick test ==="
curl -s http://localhost:11434/api/generate -d '{"model":"llama3.1:8b","prompt":"Say hello","stream":false}' | python3 -c "import sys,json; print(json.load(sys.stdin).get('response','')[:80])" 2>/dev/null || echo "Model loading..."

PRIVATE_IP=$(curl -s http://169.254.169.254/latest/meta-data/local-ipv4)
echo ""
echo "=== DONE ==="
echo "Private IP: $PRIVATE_IP"
echo ""
echo "On the gateway instance, update the Ollama URL:"
echo "  sed -i 's|WALACOR_PROVIDER_OLLAMA_URL=.*|WALACOR_PROVIDER_OLLAMA_URL=http://$PRIVATE_IP:11434|' ~/.env.gateway"
echo "  bash ~/start_gateway.sh"
