import json
import paramiko
import paho.mqtt.client as mqtt
import time
from datetime import datetime

LOG_FILE = "log_file.txt"
file = open('config.json')
data = json.load(file)

# SETUP NAT di MIKROTIK
def setup_nat_rules ():
    try :
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(data['MIKROTIK']['host'],data['MIKROTIK']['port'],data['MIKROTIK']['user'],data['MIKROTIK']['password'])

        for olt_name, cfg in data['OLTS'].items():
            # Cek NAT rule existing
            check_cmd = f'/ip firewall nat print where dst-port={cfg["port_public"]}'
            stdin, stdout, stderr = client.exec_command(check_cmd)
            existing = stdout.read().decode()

            # Jika ada rule lama, hapus dulu
            if existing.strip():
                # ambil nomor rule
                id_lines = [line.split()[0] for line in existing.splitlines() if line.strip() and line[0].isdigit()]
                for rule_id in id_lines:
                    del_cmd = f"/ip firewall nat remove numbers={rule_id}"
                    client.exec_command(del_cmd)
                    print(f"[NAT] Hapus rule lama untuk port {cfg['port_public']} (OLT {olt_name})")

            # Tambahkan rule baru
            add_cmd = (
                f'/ip firewall nat add chain=dstnat dst-address={data['MIKROTIK']['host']} '
                f'protocol=tcp dst-port={cfg["port_public"]} '
                f'action=dst-nat to-addresses={cfg["lan_ip"]} to-ports=22'
            )
            client.exec_command(add_cmd)
            print(f"[NAT] Tambah rule baru untuk {olt_name} → {cfg['lan_ip']}:{cfg['port_public']}")

        client.close()
    except Exception as e:
        print("Error setup NAT:", e)



# ==============================
# STEP 2: SSH ke OLT via NAT MIKROTIK
# ==============================
def run_command(olt, command):
    try:
        cfg = data['OLTS'][olt]
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(data['MIKROTIK']["host"], port=cfg["port_public"],
                       username=cfg["user"], password=cfg["password"])
        stdin, stdout, stderr = client.exec_command(command)
        result = stdout.read().decode()
        client.close()
        return result
    except Exception as e:
        return f"Error: {e}"


# ==============================
# STEP 3: Collect ONU Data
# ==============================
def get_onu_command(vendor: str):
    vendor = vendor.lower()
    if vendor == "huawei":
        return "display ont info summary"
    elif vendor == "zte":
        return "show gpon onu state all"
    elif vendor == "fiberhome":
        return "show pon onu-information all"
    elif vendor == "nokia":
        return "show equipment ont"
    else:
        # fallback generic
        return "show onu all"

def collect_onu_data(mqtt_client,data):
    all_data = {}
    for olt,cfg in data['OLTS']:
        # Command untuk ambil semua ONU (ubah sesuai vendor OLT)
        cmd = get_onu_command(cfg.get("vendor", "default"))
        
        output = run_command(olt, cmd)

       # Simpan ke dict
        all_data[olt] = {
            "vendor": cfg.get("vendor", "unknown"),
            "command": cmd,
            "output": output
        }

        # Publish ke MQTT
        mqtt_client.publish(
            data["MQTT"]["topic_onu"],
            json.dumps({"olt": olt, "vendor": cfg.get("vendor", "unknown"), "onu_data": output})
        )

        # Logging
        log_to_file(olt, f"ONU_DATA ({cfg.get('vendor', 'unknown')})", output)

        print(f"[ONU] Collected from {olt} ({cfg.get('vendor')})")

    return all_data

# ==============================
# STEP 4: Logging file 
# ==============================
def log_to_file(olt, tag, data):
    with open(LOG_FILE, "a") as f:
        f.write(f"[{datetime.now()}] {olt} - {tag}\n{data}\n{'='*50}\n")


# ==============================
# STEP 5: MQTT Handling mesage
# ==============================
def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        olt = payload.get("olt")
        cmd = payload.get("cmd")
        cmd_onu= cmd
        if olt in data['OLTS']:
            output = run_command(olt, cmd)
            client.publish(data['MQTT']['topic_data'], json.dumps({"olt": olt, "output": output}))
            log_to_file(olt, "CMD_EXEC", output)
    except Exception as e:
        print("Command Error:", e)

# ==============================
# MAIN
# ==============================
def get_interface_command(vendor: str):
    vendor = vendor.lower()
    if vendor == "huawei":
        return "display interface brief"
    elif vendor == "zte":
        return "show interface gpon-olt brief"
    elif vendor == "fiberhome":
        return "show interface brief"
    elif vendor == "nokia":
        return "show equipment slot"
    else:
        # fallback
        return "show interface brief"


if __name__ == "__main__":
    # Setup NAT rules (hapus duplikat + tambah baru)
    setup_nat_rules()

    # Setup MQTT
    mqtt_client = mqtt.Client()
    mqtt_client.on_message = on_message()
    mqtt_client.connect(data['MQTT']['broker'], data['MQTT']['port'], 60)
    mqtt_client.subscribe(data['MQTT']['topic_cmd'])
    mqtt_client.loop_start()

    while True:
    # Ambil status interface singkat sesuai vendor
        for olt, cfg in data['OLTS'].items():
            cmd = get_interface_command(cfg.get("vendor", "default"))
            output = run_command(olt, cmd)

            mqtt_client.publish(
            data['MQTT']['topic_data'],
            json.dumps({"olt": olt, "vendor": cfg.get("vendor"), "output": output})
         )
        log_to_file(olt, f"INTERFACE ({cfg.get('vendor')})", output)

    # Ambil data ONU tiap 30 detik
        collect_onu_data(mqtt_client, data)
        time.sleep(30)