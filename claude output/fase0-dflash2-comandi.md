# Fase 0 — llama-server + draft DFlash2, prima di toccare l'harness

Tutto quello che segue gira **sulla macchina con la RTX PRO 4500 (32 GB)**, non su quella
dell'harness. Ollama resta installato e configurato: si affianca, non si sostituisce.

Obiettivo unico di questa fase: **un numero**. Quanto accelera il draft su Blackwell.
I numeri della PR sono misurati su Apple M5 Pro (Metal), quindi su CUDA sono da dimostrare.

---

## 0. Liberare la VRAM

I due server non stanno in memoria insieme (17,6 GB + 1,1 GB contro i 22 GB del Q6_K attuale).

```bash
ollama stop qwen3.8:27b
# se la tua versione non ha "stop":
# curl http://localhost:11434/api/generate -d '{"model":"qwen3.8:27b","keep_alive":0}'

nvidia-smi --query-gpu=memory.used,memory.total --format=csv
```

Qui `nvidia-smi` è quello giusto: siamo sulla macchina del modello. (Dall'harness no — vedi
la regola in memoria: darebbe un numero credibile e falso.)

---

## 1. Build dal branch della PR

La PR #27342 è **aperta**, quindi non c'è nessun binario pronto: si compila dal fork.

```bash
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp

# Il fork z-lab/llama.cpp da cui parte la PR NON esiste piu' (404): niente
# "git remote add". Il ref della PR resta pero' sul repo upstream.
git fetch origin pull/27342/head:dflash2
git checkout dflash2

# verifica di essere sul commit giusto: dev'essere uno solo, e toccare
# tools/server/ e common/speculative*
git log --oneline -1
git diff --stat master -- | tail -5

:: La macchina GPU e' Windows: il generatore va DICHIARATO.
:: Senza -G, CMake ripiega su "NMake Makefiles" e fallisce con
::   Running 'nmake ' '-?' failed with: no such file or directory
:: Se hai gia' provato: rmdir /s /q build  (il generatore resta in CMakeCache.txt)
cmake -B build -G "Visual Studio 17 2022" -A x64 ^
  -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=120
cmake --build build --config Release -j
```

I binari finiscono in **`build\bin\Release\llama-server.exe`** (non in `build/bin/`).
Sostituisci `./build/bin/llama-server` con questo percorso in tutti i comandi qui sotto.

Prima di lanciare, controlla di avere il toolchain:

```cmd
cmake --help | findstr "Visual Studio"
```

Se non compare nessun "Visual Studio", manca il compilatore: servono i **Build Tools per
Visual Studio 2022** con il carico "Sviluppo di applicazioni desktop con C++". CUDA da solo
non basta. Se compare una versione diversa dalla 17, usa quella stringa nel `-G`.

Se hai `gh` installato, `gh pr checkout 27342` fa lo stesso: passa per il medesimo ref.

**Due cose che si pagano care se sbagliate:**

- `CMAKE_CUDA_ARCHITECTURES=120` — Blackwell è sm_120 e vuole **CUDA Toolkit 12.8 o superiore**
  (`nvcc --version` prima di partire). Con un toolkit più vecchio la build riesce lo stesso e il
  kernel per quell'architettura semplicemente non esiste: te ne accorgi a runtime, o peggio con
  un fallback lentissimo.
- `LLAMA_CURL=ON` — senza, i flag `-hf` / `-hfd` non scaricano niente da Hugging Face.

**Ordine di installazione**: il CUDA Toolkit va installato **dopo** Visual Studio, altrimenti non
registra l'integrazione con MSBuild e la build muore su un file `.cu`. Rimedio: rilanciare
l'installer CUDA e riparare i componenti "Visual Studio Integration".

**`-DLLAMA_CURL=ON` non serve piu' su questo branch**: llama.cpp ha sostituito libcurl con un
client HTTP proprio. Il sintomo, se manca il TLS, arriva **all'avvio** e non alla build:

```
get_repo_commit: error: HTTPS is not supported. Please rebuild with one of:
-DLLAMA_BUILD_BORINGSSL=ON -DLLAMA_BUILD_LIBRESSL=ON -DLLAMA_OPENSSL=ON
```

Delle tre, su Windows la meno dolorosa e' **LibreSSL** (`-DLLAMA_BUILD_LIBRESSL=ON`): si compila
da sorgente insieme a llama.cpp. BoringSSL vuole anche Go, OpenSSL vuole i dev files gia'
installati (in pratica vcpkg). Ma vedi il punto 3: scaricare i modelli a mano toglie del tutto
il bisogno di HTTPS dal server.

**Via d'uscita**: se MSVC + CUDA + branch non mergiato costano piu' di mezz'ora, **WSL2** e' una
strada seria — driver GPU in passthrough, build senza attriti, prestazioni CUDA quasi native.

---

## 2. Verificare i nomi dei flag *in questa build*

```
build\bin\Release\llama-server.exe --help | findstr /i "spec draft"
```

Non fidarti dei nomi che trovi qui sotto: la PR è aperta e può ancora cambiarli. Devi vedere
`--spec-type`, `--spec-draft-n-max`, `-hfd`. Se `--spec-type` non compare, toglilo: la PR
dichiara che DFlash2 si attiva da solo riconoscendo il checkpoint, mentre la scheda del modello
su HF lo passa esplicitamente. Le due fonti non concordano, e l'help è l'unica che conta.

---

## 3. I due modelli

Nomi esatti verificati sui repo:

| ruolo  | repo Hugging Face                        | file                          | peso    |
|--------|------------------------------------------|-------------------------------|---------|
| target | `unsloth/Qwen3.8-27B-GGUF`               | `Qwen3.8-27B-UD-Q4_K_XL.gguf` | 17,6 GB |
| draft  | `z-lab/Qwen3.8-27B-DFlash2-GGUF`         | `Qwen3.8-27B-DFlash2-Q4_K_M.gguf` | 1,14 GB |

Il file del target è **unico**, non spezzato in parti.

**Scaricali a mano**, invece di lasciarlo fare a `-hf` / `-hfd` all'avvio del server: così il
server non ha bisogno di HTTPS (vedi il punto 1), il progresso si vede, e un download caduto
riprende invece di far fallire l'avvio.

Con `curl.exe`, che su Windows c'e' di serie: niente pacchetti da installare, niente `Scripts`
da mettere nel PATH (con tre Python sulla macchina, *quale* Scripts e' pure ambiguo).

```
mkdir D:\modelli
curl -L -C - -o D:\modelli\Qwen3.8-27B-UD-Q4_K_XL.gguf https://huggingface.co/unsloth/Qwen3.8-27B-GGUF/resolve/main/Qwen3.8-27B-UD-Q4_K_XL.gguf
curl -L -C - -o D:\modelli\Qwen3.8-27B-DFlash2-Q4_K_M.gguf https://huggingface.co/z-lab/Qwen3.8-27B-DFlash2-GGUF/resolve/main/Qwen3.8-27B-DFlash2-Q4_K_M.gguf
```

`-L` segue il redirect verso la CDN; `-C -` **riprende** da dove si e' interrotto: su 17,6 GB
significa che, se cade, si rilancia la stessa identica riga.

Con la libreria (se `hf` / `huggingface-cli` non sono riconosciuti, e' il PATH): si chiama il
modulo invece dell'eseguibile, **con lo stesso interprete** per entrambi i comandi.

```
py -3.12 -m pip install -U huggingface_hub
py -3.12 -c "from huggingface_hub import hf_hub_download as d; print(d('unsloth/Qwen3.8-27B-GGUF','Qwen3.8-27B-UD-Q4_K_XL.gguf',local_dir=r'D:\modelli'))"
```

Nelle righe di lancio qui sotto: **`-m` al posto di `-hf`, `-md` al posto di `-hfd`**, con i
percorsi locali.

---

## 4. Misura A — senza draft (il metro di paragone)

In `cmd` il `\` di fine riga **non esiste** (e' bash): il continuatore e' `^`, e incollare
comandi spezzati si rompe al primo spazio di troppo. Riga singola, e per comodita' in un `.bat`:

`avvia-A.bat`

```
build\bin\Release\llama-server.exe -m D:\modelli\Qwen3.8-27B-UD-Q4_K_XL.gguf --host 0.0.0.0 --port 8080 -c 65536 -np 1 -ngl 99 -fa on --cache-type-k q8_0 --cache-type-v q8_0 --jinja --reasoning-format deepseek --no-context-shift --slots --metrics
```

Perché ogni pezzo:

- `--port 8080` — Ollama resta su 11434, convivono. `--host 0.0.0.0` perché l'harness è su
  un'altra macchina (e ricordati la regola del firewall sulla porta).
- `-np 1` — **importante**: `-c` si divide fra gli slot. Con 2 slot il contesto vero è la metà
  di quello che credi. Un harness a conversazione singola vuole uno slot solo.
- `-c 65536` — di partenza, prudente. Si alza al punto 7, dopo aver visto la VRAM occupata.
- `-fa on` + KV a `q8_0` — dimezza il costo per token del KV. Senza flash attention la KV
  quantizzata non si può usare.
- `--no-context-shift` — se il contesto finisce, preferisci un errore a una conversazione
  riscritta in silenzio. Su un harness agentico il context shift è corruzione, non degrado.
- `--jinja` — senza questo llama-server **non emette tool call**. Non è opzionale per te.
- `--reasoning-format deepseek` — il pensiero arriva in `reasoning_content`, che il tuo
  `OpenAICompatBackend` già legge e inoltra come evento `reasoning`.
- `--slots --metrics` — espongono `/slots` e `/metrics`: è da lì che in fase 2 si leggono i
  contatori del draft.

**La misura.** Serve un compito che generi *tanto*, perché lo speculative decoding non tocca il
prompt eval:

In `cmd` gli apici singoli non esistono e il JSON inline diventa un incubo di virgolette da
raddoppiare. Metti il corpo in un file e passalo con la chiocciola.

`prompt.json` (una riga sola):

```json
{"model":"qwen","messages":[{"role":"user","content":"Progetta passo per passo un algoritmo di scheduling per un pool di worker con priorità e starvation prevention. Ragiona a fondo su ogni alternativa prima di scegliere."}],"max_tokens":1200,"temperature":1.0,"top_p":0.95,"top_k":20,"stream":false}
```

```
curl -s http://localhost:8080/v1/chat/completions -H "Content-Type: application/json" -d @prompt.json
```

`jq` su Windows quasi certamente non c'e': leggi i tok/s **dal log della finestra di
llama-server** (`eval time = ... (... tokens per second)`). E' comunque la fonte piu' affidabile
delle due, il campo JSON e' solo la scorciatoia.

Annota: `predicted_per_second` (o i tok/s del log) e `nvidia-smi` a modello caricato.

---

## 5. Misura B — con il draft

Stessa identica riga, più quattro pezzi:

`avvia-B.bat` (la stessa riga di A, piu' quattro pezzi: `-md`, `--spec-type`,
`--spec-draft-n-max`, `-ngld`)

```
build\bin\Release\llama-server.exe -m D:\modelli\Qwen3.8-27B-UD-Q4_K_XL.gguf -md D:\modelli\Qwen3.8-27B-DFlash2-Q4_K_M.gguf --spec-type draft-dflash --spec-draft-n-max 7 -ngld 99 --host 0.0.0.0 --port 8080 -c 65536 -np 1 -ngl 99 -fa on --cache-type-k q8_0 --cache-type-v q8_0 --jinja --reasoning-format deepseek --no-context-shift --slots --metrics
```

Due file `.bat` invece di due comandi incollati: li lancerai decine di volte cambiando `-c` e
`--spec-draft-n-max`.

`-ngld 99` mette anche il draft in GPU: lasciarlo in CPU vanifica tutto.

Stesso curl, **stesso prompt**, stessi sampler. Ora nel JSON (o nel log) devono comparire anche
i contatori del draft — qualcosa come `draft_n` e `draft_n_accepted`. **Segnati i nomi esatti**:
sono quelli che serviranno alla spia dell'acceptance rate in fase 2, e la README di llama.cpp
non li documenta.

Guarda anche:

```bash
curl -s http://localhost:8080/slots
```

---

## 6. Il numero che decide

```
rapporto = predicted_per_second(B) / predicted_per_second(A)
```

La PR dichiara **1,85×** con acceptance 4,92 su Metal. Sotto **1,3×** su CUDA la complicazione
(build da un branch aperto, transport nuovo, backend nuovo) non si ripaga: meglio restare su
Ollama e Q6_K.

Vale la pena rifare la misura **due volte**: una a contesto vuoto, una dopo aver riempito ~30k
token di conversazione. L'acceptance rate cala quando il testo diventa meno prevedibile, e il
tuo carico vero è a contesto pieno.

---

## 7. Solo dopo: quanto contesto ci sta

Pesi: 17,6 + 1,14 = **18,7 GB**. Su 32 GB restano ~13 GB, meno i buffer di calcolo.
Con KV a `q8_0` il costo per token è circa la metà dei **0,19 MB/token** già misurati per questo
27B, quindi sui 120k teorici — ma il draft ha il suo KV e i buffer pesano.

Alza `-c` a tappe guardando `nvidia-smi` a modello caricato:

```
65536  →  98304  →  131072
```

Fermati al primo valore che lascia **almeno 1,5 GB liberi**. Quel numero è anche quello che
l'harness dovrà leggere da `/props` invece di dichiararlo da solo.

---

## Cosa serve riportare per far partire la fase 1

1. tok/s senza draft, tok/s con draft, e il rapporto
2. l'acceptance (token accettati per passo di verifica)
3. **i nomi esatti dei campi** dei contatori del draft, come li scrive questa build
4. il `-c` massimo che regge, e la VRAM occupata a quel valore
5. l'output di `curl -s http://localhost:8080/props` — da lì nasce `LlamaCppBackend`
