# Pepper Ethernet Setup

## Current working values

| | Value |
|---|---|
| Pepper IP | `10.0.0.149` |
| Laptop Ethernet interface | `enx00e04c026907` |
| Laptop Ethernet IP | `10.0.0.200/24` |
| NAOqi | `tcp://10.0.0.149:9559` |
| NetworkManager profile | `pepper-ethernet` |

> **Note:** Pepper's IP can change depending on how it is connected (direct vs switch).
> Direct link → may get `169.254.x.x` (link-local)
> Via switch → may get `192.168.210.x`
> Always verify before starting bridge.

## Create permanent profile

```bash
nmcli connection add \
  type ethernet \
  ifname enx00e04c026907 \
  con-name pepper-ethernet \
  ipv4.method manual \
  ipv4.addresses 10.0.0.200/24 \
  ipv4.routes "10.0.0.149/32" \
  ipv4.never-default yes \
  ipv6.method ignore
```

## Activate / deactivate

```bash
nmcli connection up pepper-ethernet
nmcli connection down pepper-ethernet
nmcli connection delete pepper-ethernet
```

## Verify connectivity

```bash
ip route get 10.0.0.149
nc -vz -s 10.0.0.200 10.0.0.149 9559
```

> Use `nc` to port 9559, not ping. Ping may fail even when Ethernet works.
