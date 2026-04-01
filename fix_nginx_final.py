#!/usr/bin/env python3
import sys

def replace_location_blocks():
    """Replace /hott/ and /watt/ location blocks to serve from filesystem"""

    # Read the config
    with open("/etc/nginx/conf.d/living_network.conf", "r") as f:
        lines = f.readlines()

    # Find and replace /hott/ block
    hott_start = None
    hott_end = None

    for i, line in enumerate(lines):
        if "location /hott/" in line:
            hott_start = i
        elif hott_start is not None and hott_end is None:
            if line.strip() == "}" and i >= hott_start + 30:
                hott_end = i
                break

    # Find and replace /watt/ block
    watt_start = None
    watt_end = None

    if hott_end is not None:
        for i in range(hott_end + 1, len(lines)):
            line = lines[i]
            if "location /watt/" in line:
                watt_start = i
            elif watt_start is not None and watt_end is None:
                if line.strip() == "}" and i >= watt_start + 30:
                    watt_end = i
                    break
    # Create new blocks - simpler structure without proxy
    hott_new_block = [
        "    # Strand 0 (A) — Point [volatile]  α=+0.0153  verts=1\n",
        "    location /hott/ {\n",
        "        alias /home/hott/;\n",
        "        index index.html;\n",
        "        autoindex on;\n",
        "\n",
        "        # φ-structured cache\n",
        "        expires 49m;\n",
        "        add_header Cache-Control \"public, max-age=2940\";\n",
        "\n",
        "        # HDGL observability headers\n",
        "        add_header X-HDGL-Strand      \"0\"   always;\n",
        "        add_header X-HDGL-Polytope    \"Point\"         always;\n",
        "        add_header X-HDGL-Stability   \"volatile\"    always;\n",
        "        add_header Accept-Ranges      bytes always;\n",
        "\n",
        "        location ~ /\\. { deny all; }\n",
        "    }\n",
    ]

    watt_new_block = [
        "    # Strand 0 (A) — Point [volatile]  α=+0.0153  verts=1\n",
        "    location /watt/ {\n",
        "        alias /home/watt/;\n",
        "        index index.html;\n",
        "        autoindex on;\n",
        "\n",
        "        # φ-structured cache\n",
        "        expires 49m;\n",
        "        add_header Cache-Control \"public, max-age=2940\";\n",
        "\n",
        "        # HDGL observability headers\n",
        "        add_header X-HDGL-Strand      \"0\"   always;\n",
        "        add_header X-HDGL-Polytope    \"Point\"         always;\n",
        "        add_header X-HDGL-Stability   \"volatile\"    always;\n",
        "        add_header Accept-Ranges      bytes always;\n",
        "\n",
        "        location ~ /\\. { deny all; }\n",
        "    }\n",
    ]

    # Build new config
    new_lines = (
        lines[:hott_start] +
        hott_new_block +
        lines[hott_end + 1:watt_start] +
        watt_new_block +
        lines[watt_end + 1:]
    )

    # Write to temp file
    with open("/tmp/living_network.conf.updated", "w") as f:
        f.writelines(new_lines)

    # Test the configuration
    import subprocess
    result = subprocess.run(
        ["nginx", "-t", "-c", "/tmp/living_network.conf.updated"],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        print("ERROR: NGINX config validation failed")
        print(result.stderr)
        return False

    print("✓ Config validation passed")

    # Apply the new config
    import shutil
    shutil.copy("/tmp/living_network.conf.updated", "/etc/nginx/conf.d/living_network.conf")
    print("✓ Config file updated")

    # Reload NGINX
    result = subprocess.run(["systemctl", "reload", "nginx"], capture_output=True, text=True)
    if result.returncode != 0:
        print("ERROR: Failed to reload NGINX")
        print(result.stderr)
        return False

    print("✓ NGINX reloaded successfully")
    return True

if __name__ == "__main__":
    try:
        success = replace_location_blocks()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
