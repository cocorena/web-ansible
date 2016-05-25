#!/bin/bash
#Ansible Ping entire environment

ansible -vvvv -u root -i scripts/rax.py web -m ping -f 20
