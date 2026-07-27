import re
def _normalize(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r'[^\w\s]', ' ', s.lower())
    return ' '.join(s.split())

def _get_ngrams(words: list[str], n: int) -> set[str]:
    ngrams = set()
    for i in range(len(words) - n + 1):
        ngrams.add(' '.join(words[i:i+n]))
    return ngrams

text = "Purchase the Ashraya domain"
norm_text = _normalize(text)
words = norm_text.split()
text_ngrams = set()
for i in range(1, 5):
    text_ngrams.update(_get_ngrams(words, i))

orgs = [
    {"id":"39608da8-cfdf-48c4-8b8f-e4993094cec9","name":"Ashraya"},
    {"id":"ee0b3b10-4fed-42bf-99db-6d9b46a5d09f","name":"Ashraya India"},
    {"id":"3fd658c4-3536-4a05-b52b-1a17520b7649","name":"Ashraya Chennai North"},
    {"id":"786aeb42-c759-4801-a575-471b42f315c3","name":"Ashraya Chennai"},
    {"id":"775b560f-6ebe-4bbe-a1ec-d7d0f0acae90","name":"Ashraya Chennai Central"},
    {"id":"aa4cd391-6827-4a44-ba20-12f444831ff6","name":"Ashraya Chennai South"}
]

matched = []
for org in orgs:
    norm_name = _normalize(org['name'])
    if norm_name in text_ngrams:
        matched.append(org['name'])

print("Matched Orgs:", matched)
