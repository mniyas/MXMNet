# Install requirements
grep -v '^#' requirements.txt | xargs -n 1 -L 1 pip install --default-timeout=100 --no-cache-dir
# Create a checkpoint folder
mkdir checkpoint