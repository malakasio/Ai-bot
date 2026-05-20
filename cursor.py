import sys, os, requests, subprocess

# API Key and URL set via environment variables
KEY = os.environ.get("ANTHROPIC_API_KEY")
if not KEY:
    print("❌ Error: ANTHROPIC_API_KEY not set in environment")
    sys.exit(1)

URL = "https://api.anthropic.com/v1/messages"

# Model name via environment, with fallback
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")


def run_agent():
    if len(sys.argv) < 2:
        print("❌ Χρήση: python cursor.py \"οδηγία\"")
        return

    instruction = sys.argv[1]

    # 1. Βλέπει όλα τα αρχεία του φακέλου
    all_files = [f for f in os.listdir('.') if os.path.isfile(f) and f.endswith(('.py', '.txt', '.json'))]
    files_list = "\n".join(all_files)

    print(f"🕵️ Ο {MODEL} αναλύει το Repo σου...")

    headers = {"x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}

    # 2. Πρώτο αίτημα: "Ποιο αρχείο πρέπει να πειράξω;"
    selector_prompt = f"Στο repo υπάρχουν αυτά τα αρχεία:\n{files_list}\n\nΟ χρήστης θέλει: {instruction}\nΠοιο ΕΝΑ αρχείο είναι το πιο σχετικό για να αλλάξω; Απάντησε ΜΟΝΟ με το όνομα του αρχείου (π.χ. main.py)."

    try:
        selector_data = {
            "model": MODEL, "max_tokens": 50,
            "messages": [{"role": "user", "content": selector_prompt}]
        }
        selector_response = requests.post(URL, headers=headers, json=selector_data)
        selector_response.raise_for_status()
        selector_json = selector_response.json()

        if 'content' not in selector_json or len(selector_json['content']) == 0:
            print("⚠️ Κενή απάντηση από API. Δοκιμάζω main.py.")
            target_file = "main.py"
        else:
            target_file = selector_json['content'][0]['text'].strip()

        if target_file not in all_files:
            print(f"⚠️ Ο {MODEL} πρότεινε το '{target_file}', αλλά δεν το βρίσκω. Δοκιμάζω το main.py.")
            target_file = "main.py"

        print(f"🎯 Επιλέχθηκε το αρχείο: {target_file}")

        # 3. Δεύτερο αίτημα: Κάνε την αλλαγή στο αρχείο που επιλέχθηκε
        with open(target_file, 'r', encoding='utf-8') as f:
            old_code = f.read()

        print(f"🤖 Επεξεργασία του {target_file}...")
        edit_data = {
            "model": MODEL, "max_tokens": 4000,
            "system": "Επίστρεψε ΑΠΟΚΛΕΙΣΤΙΚΑ τον διορθωμένο κώδικα, χωρίς εξηγήσεις ή markdown.",
            "messages": [{"role": "user", "content": f"Άλλαξε αυτόν τον κώδικα σύμφωνα με την οδηγία: {instruction}\n\nΚώδικας:\n{old_code}"}]
        }
        edit_response = requests.post(URL, headers=headers, json=edit_data)
        edit_response.raise_for_status()
        edit_json = edit_response.json()

        if 'content' not in edit_json or len(edit_json['content']) == 0:
            print("❌ Κενή απάντηση από API για την επεξεργασία")
            return

        new_code = edit_json['content'][0]['text'].strip()

        with open(target_file, 'w', encoding='utf-8') as f:
            f.write(new_code)

        print(f"✅ Το {target_file} ενημερώθηκε.")

        # 4. Push στο GitHub
        subprocess.run(f"git add {target_file}", shell=True)
        subprocess.run(f"git commit -m 'Smart Cursor: {instruction[:30]}'", shell=True)
        subprocess.run("git push origin main", shell=True)
        print("🎉 Επιτυχία! Το Repo ενημερώθηκε αυτόματα.")

    except Exception as e:
        print(f"❌ Σφάλμα: {e}")


if __name__ == "__main__":
    run_agent()
