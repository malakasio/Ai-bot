import sys, os, requests, subprocess

KEY = "sk-ant-api03-8MPBrKilzTD3kn8tBEjHIPPzVPzoqtx3mqKHopEgdfzAucapWCNwJ5-q7PmKQdovlwDDUhgfhJ2uu3BpF3I7aT-QEi09Abb"
URL = "https://anthropic-api.com/v1/messages"
MODEL = "claude-opus-4.7"

def run_agent():
    if len(sys.argv) < 3:
        print("❌ Χρήση: python fix_and_push.py <αρχείο> \"οδηγία\"")
        return
    target_file = sys.argv[1]
    instruction = sys.argv[2]
    if not os.path.exists(target_file):
        print(f"❌ Το αρχείο {target_file} δεν υπάρχει!")
        return
    with open(target_file, 'r', encoding='utf-8') as f:
        code = f.read()
    print(f"🤖 Ο Claude επεξεργάζεται το {target_file}...")
    headers = {
        "x-api-key": KEY, 
        "anthropic-version": "2023-06-01", 
        "content-type": "application/json"
    }
    data = {
        "model": MODEL,
        "max_tokens": 4000,
        "system": "Επίστρεψε ΑΠΟΚΛΕΙΣΤΙΚΑ ΚΑΙ ΜΟΝΟ τον διορθωμένο κώδικα. Χωρίς markdown, χωρίς ```python, χωρίς επεξηγήσεις.",
        "messages": [{"role": "user", "content": f"Κάνε αυτή την αλλαγή: {instruction}\n\nΚώδικας:\n{code}"}]
    }
    try:
        response = requests.post(URL, headers=headers, json=data)
        res = response.json()
        if 'content' in res:
            new_code = res['content'][0]['text'].strip()
        else:
            print(f"❌ Σφάλμα API: {res}")
            return
    except Exception as e:
        print(f"❌ Σφάλμα σύνδεσης: {e}")
        return
    with open(target_file, 'w', encoding='utf-8') as f:
        f.write(new_code)
    print(f"✅ Οι αλλαγές εφαρμόστηκαν στο {target_file}.")
    print("🚀 Ξεκινάει το Push στο GitHub...")
    subprocess.run(f"git add {target_file}", shell=True)
    subprocess.run(f"git commit -m 'Agent Fix: {instruction[:20]}'", shell=True)
    subprocess.run("git push origin main", shell=True)
    print("🎉 Η διαδικασία ολοκληρώθηκε!")

if __name__ == "__main__":
    run_agent()
