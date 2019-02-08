sudo certbot \
    -a dns-digitalocean \
    -i nginx \
    -d "*.reckonsys.xyz" \
    -d reckonsys.xyz \
    --server https://acme-v02.api.letsencrypt.org/directory
