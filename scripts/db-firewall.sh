#!/usr/bin/env bash
set -euo pipefail
# Restrict only this application's Docker ports; keep SSH rules untouched.
iface=$(ip -o route get 192.0.2.10 | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1);exit}}')
[[ -n "$iface" ]] || exit 1
iptables -w -N ECR_DB 2>/dev/null || true
for port in 5432 6379; do
  iptables -w -C ECR_DB -s 192.0.2.10/32 -p tcp -m conntrack --ctorigdst 192.0.2.20 --ctorigdstport "$port" -j RETURN 2>/dev/null ||
    iptables -w -A ECR_DB -s 192.0.2.10/32 -p tcp -m conntrack --ctorigdst 192.0.2.20 --ctorigdstport "$port" -j RETURN
  iptables -w -C ECR_DB -p tcp -m conntrack --ctorigdst 192.0.2.20 --ctorigdstport "$port" -j DROP 2>/dev/null ||
    iptables -w -A ECR_DB -p tcp -m conntrack --ctorigdst 192.0.2.20 --ctorigdstport "$port" -j DROP
done
iptables -w -C ECR_DB -j RETURN 2>/dev/null || iptables -w -A ECR_DB -j RETURN
iptables -w -N DOCKER-USER 2>/dev/null || true
iptables -w -C FORWARD -i "$iface" -j ECR_DB 2>/dev/null || iptables -w -I FORWARD 1 -i "$iface" -j ECR_DB
iptables -w -C DOCKER-USER -i "$iface" -j ECR_DB 2>/dev/null || iptables -w -I DOCKER-USER 1 -i "$iface" -j ECR_DB
# Also cover Docker userland-proxy traffic terminating on the host.
iptables -w -N ECR_DB_INPUT 2>/dev/null || true
iptables -w -C ECR_DB_INPUT -s 192.0.2.10/32 -j RETURN 2>/dev/null || iptables -w -A ECR_DB_INPUT -s 192.0.2.10/32 -j RETURN
iptables -w -C ECR_DB_INPUT -j DROP 2>/dev/null || iptables -w -A ECR_DB_INPUT -j DROP
iptables -w -C INPUT -i "$iface" -d 192.0.2.20 -p tcp -m multiport --dports 5432,6379 -j ECR_DB_INPUT 2>/dev/null ||
  iptables -w -I INPUT 1 -i "$iface" -d 192.0.2.20 -p tcp -m multiport --dports 5432,6379 -j ECR_DB_INPUT
