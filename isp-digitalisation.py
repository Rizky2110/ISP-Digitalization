import json
import paramiko
import paho.mqtt.client as mqtt
import time
from datetime import datetime

# ==============================
# CONFIG
# ==============================
LOG_FILE = "log_file.txt"

with open('config.json') as file:
    data = json.load(file)


# ==============================
# STEP 1: NAT Management di MikroTik
# ==============================
def setup_nat_rules():
    """Setup NAT rules di MikroTik: hapus duplikat, tambah rule baru."""
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(data['MIKROTIK']['host'],
                       data['MIKROTIK']['port'],
                       data['MIKROTIK']['user'],
                       data['MIKROTIK']['password'])

        for olt_name, cfg in data['OLTS'].items():
            # Cek NAT rule existing
            check_cmd = f'/ip firewall nat print where dst-port={cfg["port_public"]}'
            _, stdout, _ = client.exec_command(check_cmd)
            existing = stdout.read().decode()

            # Jika ada rule lama, hapus
            if existing.strip():
                id_lines = [
                    line.split()[0]
                    for line in existing.splitlines()
                    if line.strip() and line[0].isdigit()
                ]
                for rule_id in id_lines:
                    del_cmd = f"/ip firewall nat remove numbers={rule_id}"
                    client.exec_command(del_cmd)
                    print(f"[NAT] Hapus rule lama port {cfg['port_public']} (OLT {olt_name})")

            # Tambah rule baru
            add_cmd = (
                f"/ip firewall nat add chain=dstnat dst-address={data['MIKROTIK']['host']} "
                f"protocol=tcp dst-port={cfg['port_public']} "
                f"action=dst-nat to-addresses={cfg['lan_ip']} to-ports=22"
            )
            client.exec_command(add_cmd)
            print(f"[NAT] Tambah rule baru {olt_name} → {cfg['lan_ip']}:{cfg['port_public']}")

        client.close()
    except Exception as e:
        print("Error setup NAT:", e)


# ==============================
# STEP 2: SSH ke OLT via NAT MikroTik
# ==============================
def run_command(olt, command):
    """Eksekusi command ke OLT via NAT MikroTik."""
    try:
        cfg = data['OLTS'][olt]
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(data['MIKROTIK']["host"], port=cfg["port_public"],
                       username=cfg["user"], password=cfg["password"])
        _, stdout, stderr = client.exec_command(command)
        result = stdout.read().decode() + stderr.read().decode()
        client.close()
        return result.strip()
    except Exception as e:
        return f"Error run_command: {e}"


# ==============================
# STEP 3: Command Mapping
# ==============================
def get_onu_command(vendor: str):
    """Pilih command collect ONU sesuai vendor."""
    vendor = vendor.lower()
    return {
        "huawei": "display ont info summary",
        "zte": "show gpon onu state all",
        "fiberhome": "show pon onu-information all",
        "nokia": "show equipment ont"
    }.get(vendor, "show onu all")


def get_interface_command(vendor: str):
    """Pilih command interface sesuai vendor."""
    vendor = vendor.lower()
    return {
        "huawei": "display interface brief",
        "zte": "show interface gpon-olt brief",
        "fiberhome": "show interface brief",
        "nokia": "show equipment slot"
    }.get(vendor, "show interface brief")


# ==============================
# STEP 4: Collect ONU Data
# ==============================
def collect_onu_data(mqtt_client):
    """Ambil data ONU dari semua OLT, publish ke MQTT & log."""
    all_data = {}
    for olt, cfg in data['OLTS'].items():
        cmd = get_onu_command(cfg.get("vendor", "default"))
        output = run_command(olt, cmd)

        all_data[olt] = {"vendor": cfg.get("vendor", "unknown"),
                         "command": cmd,
                         "output": output}

        mqtt_client.publish(data["MQTT"]["topic_onu"],
                            json.dumps({"olt": olt, "vendor": cfg.get("vendor", "unknown"), "onu_data": output}))
        log_to_file(olt, f"ONU_DATA ({cfg.get('vendor', 'unknown')})", output)
        print(f"[ONU] Collected {olt} ({cfg.get('vendor')})")
    return all_data


# ==============================
# STEP 5: Logging
# ==============================
def log_to_file(olt, tag, content):
    """Simpan hasil ke log file dengan timestamp."""
    with open(LOG_FILE, "a") as f:
        f.write(f"[{datetime.now()}] {olt} - {tag}\n{content}\n{'='*50}\n")


# ==============================
# STEP 6: MQTT Handling
# ==============================
def on_message(client, userdata, msg):
    """Handler command via MQTT."""
    try:
        payload = json.loads(msg.payload.decode())
        olt = payload.get("olt")
        cmd = payload.get("cmd")

        if olt in data['OLTS']:
            output = run_command(olt, cmd)
            client.publish(data['MQTT']['topic_cmd_result'],
                           json.dumps({"olt": olt, "cmd": cmd, "result": output}))
            log_to_file(olt, f"CMD_EXEC {cmd}", output)
            print(f"[CMD] {olt} → {cmd}")
        else:
            print(f"[ERROR] OLT {olt} tidak ditemukan!")
    except Exception as e:
        print("Command Error:", e)


# ==============================
# MAIN
# ==============================
if __name__ == "__main__":
    setup_nat_rules()

    # Setup MQTT
    mqtt_client = mqtt.Client()
    mqtt_client.on_message = on_message
    mqtt_client.connect(data['MQTT']['broker'], data['MQTT']['port'], 60)
    mqtt_client.subscribe(data['MQTT']['topic_cmd'])
    mqtt_client.loop_start()

    while True:
        # Ambil status interface tiap OLT
        for olt, cfg in data['OLTS'].items():
            cmd = get_interface_command(cfg.get("vendor", "default"))
            output = run_command(olt, cmd)

            mqtt_client.publish(data['MQTT']['topic_data'],
                                json.dumps({"olt": olt, "vendor": cfg.get("vendor"), "output": output}))
            log_to_file(olt, f"INTERFACE ({cfg.get('vendor')})", output)

        # Ambil data ONU tiap 30 detik
        collect_onu_data(mqtt_client)
        time.sleep(30)
