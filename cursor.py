import sys, os, requests, subprocess

KEY = "sk-ant-api03-8MPBrKilzTD3kn8tBEjHIPPzVPzoqtx3mqKHopEgdfzAucapWCNwJ5-q7PmKQdovlwDDUhgfhJ2uu3BpF3I7aT-QEi09Abb"
URL = "https://anthropic-api.com/v1/messages"
MODEL = "claude-opus-4-7"

def run_agent():
    if len(sys.argv) < 2:
        print("❌ Χρήση: python cursor.py \"οδηγία\"")
        return
    
    instruction = sys.argv[1]
    
    # 1. Βλέπει όλα τα αρχεία του φακέλου
    all_files = [f for f in os.listdir('.') if os.path.isfile(f) and f.endswith(('.py', '.txt', '.json'))]
    files_list = "\n".join(all_files)
    
    print(f"🕵️ Ο Opus 4.7 αναλύει το Repo σου...")
    
    headers = {"x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    
    # 2. Πρώτο αίτημα: "Ποιο αρχείο πρέπει να πειράξω;"
    selector_prompt = f"Στο repo υπάρχουν αυτά τα αρχεία:\n{files_list}\n\nΟ χρήστης θέλει: {instruction}\nΠοιο ΕΝΑ αρχείο είναι το πιο σχετικό για να αλλάξω; Απάντησε ΜΟΝΟ με το όνομα του αρχείου (π.χ. main.py)."
    
    try:
        selector_data = {
            "model": MODEL, "max_tokens": 50,
            "messages": [{"role": "user", "content": selector_prompt}]
        }
        target_file = requests.post(URL, headers=headers, json=selector_data).json()['content'][0]['text'].strip()
        
        if target_file not in all_files:
            print(f"⚠️ Ο Claude πρότεινε το '{target_file}', αλλά δεν το βρίσκω. Δοκιμάζω το main.py.")
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
        new_code = requests.post(URL, headers=headers, json=edit_data).json()['content'][0]['text'].strip()
        
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
