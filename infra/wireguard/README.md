# WireGuard: связь billing-VPS ↔ VPN-VPS

Документация для **Фазы B**. Billing-сервер обращается к панели управления учётными записями только по приватному туннелю.

## Схема

| Узел | IP в туннеле | Роль |
|------|--------------|------|
| VPN-VPS (`132.243.123.55`) | `10.10.0.1` | Marzban API, проброс порта 8000 на wg0 |
| Billing-VPS (новый) | `10.10.0.2` | VXN_Pay API, `MARZBAN_BASE_URL=http://10.10.0.1:8000` |

Подсеть: `10.10.0.0/24`

## SSH-доступ

Два разных ключа — **не путать**:

| Сервер | IP | SSH-ключ |
|--------|-----|----------|
| VPN-VPS (VXN Cloud, Marzban) | `132.243.123.55` | `id_ed25519_firstvds_jump1` |
| Billing-VPS (VXN_Pay) | `<BILLING_VPS_IP>` | `id_ed25519_tipmycode_wmrs_vps` |

Приватные ключи не коммитить в git.

```powershell
# VPN-VPS
ssh -i $env:USERPROFILE\.ssh\id_ed25519_firstvds_jump1 root@132.243.123.55
# или alias из ~/.ssh/config:
ssh firstvds-jump1

# Billing-VPS (когда будет развёрнут)
ssh -i $env:USERPROFILE\.ssh\id_ed25519_tipmycode_wmrs_vps root@<BILLING_VPS_IP>
```

Рекомендуется добавить в `~/.ssh/config`:

```
Host vxn-vpn
    HostName 132.243.123.55
    User root
    IdentityFile ~/.ssh/id_ed25519_firstvds_jump1

Host vxn-billing
    HostName <BILLING_VPS_IP>
    User root
    IdentityFile ~/.ssh/id_ed25519_tipmycode_wmrs_vps
```

## Локальная разработка без WireGuard

Пока billing-VPS не развёрнут, можно тестировать API с ПК через SSH-туннель к VPN-VPS:

```powershell
# Терминал 1 — туннель к VPN-VPS (оставить открытым)
# Ключ firstvds_jump1 — VPN-сервер, не tipmycode!
ssh -i $env:USERPROFILE\.ssh\id_ed25519_firstvds_jump1 -N -L 18000:127.0.0.1:8000 root@132.243.123.55
# или: ssh -N -L 18000:127.0.0.1:8000 firstvds-jump1
```

В `.env` для Docker на Windows:

```env
MARZBAN_BASE_URL=http://host.docker.internal:18000
MARZBAN_ADMIN_USER=admin
MARZBAN_ADMIN_PASSWORD=<пароль из install/SECRETS.local.md на VPN-VPS>
```

## Установка WireGuard

### 1. VPN-VPS

```bash
ssh vxn-vpn
apt update && apt install -y wireguard
```

Сгенерировать ключи:

```bash
wg genkey | tee /etc/wireguard/server_private.key | wg pubkey > /etc/wireguard/server_public.key
chmod 600 /etc/wireguard/server_private.key
```

Скопировать `vpn-vps-wg0.conf.example` → `/etc/wireguard/wg0.conf`, подставить:
- `PrivateKey` — из `server_private.key`
- `PublicKey` billing peer — с billing-VPS

```bash
systemctl enable --now wg-quick@wg0
```

### 2. Billing-VPS

Аналогично, конфиг из `billing-vps-wg0.conf.example`.

Проверка с billing-VPS:

```bash
ping -c 3 10.10.0.1
curl -s http://10.10.0.1:8000/api/admin/token  # должен ответить 422/405, не timeout
```

## Проброс Marzban API на wg0 (VPN-VPS)

Marzban слушает `127.0.0.1:8000`. Нужен проброс только на интерфейс WireGuard.

### Вариант A: socat (рекомендуется)

Файл `marzban-bridge-socat.service.example` → `/etc/systemd/system/marzban-bridge.service`

```bash
systemctl daemon-reload
systemctl enable --now marzban-bridge
```

### Вариант B: изменить UVICORN_HOST

В `/opt/marzban/.env` (осторожно — только с UFW-ограничением):

```env
UVICORN_HOST=10.10.0.1
```

UFW — разрешить 8000 **только** с `10.10.0.0/24`:

```bash
ufw allow from 10.10.0.0/24 to any port 8000 proto tcp
```

**Не открывать** порт 8000 в публичный интернет.

## Проверка Фазы B

1. Заполнить `MARZBAN_ADMIN_PASSWORD` в `.env`
2. Поднять туннель или WireGuard
3. Admin-токен → `GET /api/v1/admin/bridge/status`
4. Синхронизация → `POST /api/v1/admin/connections/sync-all`
5. Продление → `POST /api/v1/admin/connections/{name}/extend` с `{"period_days": 30}`

## Безопасность

- Пароль панели — только в `.env`, не в git
- WireGuard ключи — только на серверах
- SSH-ключи — права `600` на приватные файлы; VPN и billing используют **разные** ключи
- Marzban API не публикуется в интернет
