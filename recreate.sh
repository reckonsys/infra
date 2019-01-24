vagrant destroy -f
vagrant up
ssh-keygen -f ~/.ssh/known_hosts -R [127.0.0.1]:2222
cat ~/.ssh/id_rsa.pub | vagrant ssh -c "cat >> .ssh/authorized_keys"
ssh -T vagrant@127.0.0.1 -p 2222
