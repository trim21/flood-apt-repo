```shell
echo "deb [trusted=yes] https://flood-apt-repo.pages.dev/ devel main" | sudo tee /etc/apt/sources.list.d/flood.list

sudo apt-get update -y
sudo apt-get install flood -y
flood --version
```
