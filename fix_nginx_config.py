#!/usr/bin/env python3
import shutil
import subprocess
import sys

def fix_nginx_config():
    try:
        # Read current config
        with open("/etc/nginx/conf.d/living_network.conf", "r") as f:
            lines = f.readlines()

        # Read replacement blocks
        with open("/tmp/hott_block.txt", "r") as f:
            hott_lines = f.readlines()

        with open("/tmp/watt_block.txt", "r") as f:
            watt_lines = f.readlines()

        # Find /hott/ block (starts around line 94, index 93)
        hott_start = 93
        hott_end = None
        for i in range(hott_start, min(hott_start + 60, len(lines))):
            if lines[i].strip() == "}" and i > hott_start + 5:
                hott_end = i
                break

        if hott_end is None:
            print("ERROR: Could not find end of /hott/ block")
            return False

        # Find /watt/ block
        watt_start = None
        for i in range(hott_end + 1, min(hott_end + 70, len(lines))):
            if "location /watt/" in lines[i]:
                watt_start = i
                break

        if watt_start is None:
            print("ERROR: Could not find /watt/ block")
            return False

        # Find end of /watt/ block
        watt_end = None
        for i in range(watt_start + 1, len(lines)):
            if lines[i].strip() == "}" and i > watt_start + 5:
                watt_end = i
                break

        if watt_end is None:
            print("ERROR: Could not find end of /watt/ block")
            return False

        print(f"Found /hott/ block: lines {hott_start+1}-{hott_end+1}")
        print(f"Found /watt/ block: lines {watt_start+1}-{watt_end+1}")

        # Build new config by combining parts
        new_lines = (
            lines[:hott_start] +
            hott_lines +
            lines[hott_end+1:watt_start] +
            watt_lines +
            lines[watt_end+1:]
        )

        # Write to temporary file
        with open("/tmp/living_network.conf.new", "w") as f:
            f.writelines(new_lines)

        # Validate with nginx -t
        result = subprocess.run(
            ["nginx", "-t", "-c", "/tmp/living_network.conf.new"],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            print("ERROR: NGINX config validation failed")
            print(result.stderr)
            return False

        print("Config validation: SUCCESS")

        # Apply the new config
        shutil.copy("/tmp/living_network.conf.new", "/etc/nginx/conf.d/living_network.conf")
        print("Config file updated")

        # Reload NGINX
        result = subprocess.run(["systemctl", "reload", "nginx"], capture_output=True, text=True)
        if result.returncode != 0:
            print("ERROR: Failed to reload NGINX")
            print(result.stderr)
            return False

        print("NGINX reloaded successfully")
        return True

    except Exception as e:
        print(f"ERROR: {e}")
        return False

if __name__ == "__main__":
    success = fix_nginx_config()
    sys.exit(0 if success else 1)
