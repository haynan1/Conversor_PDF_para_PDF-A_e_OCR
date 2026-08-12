# Scriptor

**OCR e conversão PDF/A com verificação de conformidade e trilha de auditoria.**

Transforma digitalizações em PDF/A pesquisável, com as garantias que um acervo
exige: nada é convertido em silêncio, nada é apagado sem confirmação, e todo
arquivo produzido é verificado antes de ser aceito.

Sucessor do kit de scripts `.bat` que processava `C:\PDF_Automacao`.

---

## Para usar (sem linha de comando)

**Duplo clique em `Scriptor.cmd`.**

Na primeira vez ele prepara o ambiente sozinho — leva cerca de um minuto — e
depois abre a interface no navegador. Nas execuções seguintes, abre direto.

Na interface: arraste os documentos para a área indicada, confira o que está na
fila e clique em **Converter**. Os PDF/A aparecem na pasta de saída, e os
originais vão para *Processados*.

> **Se aparecer um aviso vermelho** dizendo que falta o Tesseract ou o
> Ghostscript, o próprio aviso indica qual arquivo da pasta `instaladores`
> executar. Não é necessário mexer no PATH do Windows.
>
> **Se o Python não estiver instalado**, o `Scriptor.cmd` avisa e abre a pasta
> com o instalador. Marque *"Add python.exe to PATH"* na primeira tela.

O que a interface permite ajustar: idioma do reconhecimento, perfil de
arquivamento (PDF/A-1b, 2b, 3b), destino dos originais, tratamento de documentos
assinados e correções de imagem. Tudo é gravado no `scriptor.toml`, que continua
legível e editável à mão.

---

## Para automatizar (linha de comando)

```powershell
pip install -e .
scriptor doctor        # diagnostica o ambiente e diz o que corrigir
scriptor run           # converte a pasta de entrada
```

| Comando | O que faz |
| --- | --- |
| `abrir` | Abre a interface gráfica. |
| `run` | Converte a pasta de entrada. É o padrão quando nenhum comando é dado. |
| `doctor` | Diagnostica o ambiente e aponta a correção de cada problema. |
| `watch` | Vigia a entrada e processa arquivos assim que terminam de ser gravados. |
| `inspecionar ARQUIVO` | Mostra o perfil do documento e a estratégia que seria aplicada. |
| `verificar ALVO` | Confere a conformidade PDF/A de arquivos já existentes. |
| `historico` | Lê a trilha de auditoria do ledger. |
| `limpar` | Remove arquivos antigos das pastas gerenciadas. Lista por padrão; remove só com `--confirmar`. |
| `init` | Cria um workspace novo. |

```powershell
scriptor run --idioma por+eng      # documentos bilíngues
scriptor run --perfil pdfa-1       # exigência de PDF/A-1b
scriptor run --simular             # mostra o plano, não converte
scriptor run --forcar              # reprocessa o que já está no ledger
```

Precedência de configuração: flags de CLI > variáveis `SCRIPTOR_*` >
`scriptor.toml` > padrões. Caminhos relativos são resolvidos a partir da pasta
do `scriptor.toml`, então o workspace inteiro pode ser copiado para outra
máquina sem ajuste nenhum.

---

## O que mudou em relação ao kit `.bat`

### Estratégia decidida por documento, não por lote

O script antigo aplicava `--skip-text` a todo arquivo. Essa flag pula qualquer
página que contenha *qualquer* texto — e o efeito prático é severo:

| Digitalização com carimbo "Fls. 12" | Página 1 | Página 2 |
| --- | --- | --- |
| `.bat` original (`--skip-text`) | **7 caracteres** — só o carimbo | 602 |
| Scriptor (`--redo-ocr`) | **611 caracteres** — página inteira | 602 |

Seiscentos caracteres perdidos em silêncio, por página carimbada. Em documento
administrativo brasileiro isso é a regra: toda folha de processo leva carimbo de
numeração.

O Scriptor mede cada documento antes de agir — quantas páginas têm camada de
texto real acima de um limiar configurável, quantas têm só resíduo, quantas são
puro bitmap, se há assinatura digital, se já é PDF/A:

| Natureza medida | Estratégia | Motivo |
| --- | --- | --- |
| Digitalização pura | `--skip-text` | Nada a preservar; OCR em tudo. |
| Digitalização com resíduo | `--redo-ocr` | Carimbo não é camada de texto. |
| Nativo digital | `--skip-text` | Texto vetorial íntegro; só empacota em PDF/A. |
| Misto | `--redo-ocr` | Preserva o texto existente, OCRiza apenas as imagens. |
| Bitmap (TIFF/JPEG) | `--skip-text` + `--image-dpi` | Entrada que o kit antigo nem aceitava. |

`--force-ocr` existe apenas como último degrau da escada de recuperação: é a
única opção que pode *piorar* um documento, porque rasteriza a página inteira.

### Falha é tratada como falha

O `.bat` ignorava o código de saída do OCRmyPDF e imprimia "Conversao
concluida!" independentemente do resultado. Aqui cada um dos treze códigos é
classificado:

- **fatal** — dependência ausente, PDF criptografado, acesso negado: aborta;
- **degradável** — a conversão PDF/A falhou no Ghostscript: repete o mesmo modo
  sem otimização, tolerando erro leve de renderização;
- **escalável** — sobe um degrau na escada de estratégias.

### Verificação antes da entrega

O kit prometia PDF/A e nunca conferia. O Scriptor valida cada arquivo produzido
— identificação XMP, OutputIntent, ausência de criptografia, embutimento de
todas as fontes — e trata a reprovação como falha de conversão, alimentando a
escada de recuperação em vez de entregar um arquivo não conforme. Com veraPDF
instalado, a palavra final é do validador de referência.

### Assinatura digital é respeitada

OCR reescreve o PDF e invalida qualquer assinatura digital nele. O script antigo
fazia isso em silêncio. O padrão aqui é **pular** documentos assinados e
reportá-los; converter exige escolha explícita, com a consequência declarada.

### Nada é destruído

`lixo.bat` executava `del /Q` na entrada **e** na saída, sem filtro e sem
confirmação. Seu substituto, `scriptor limpar`, lista por padrão, filtra por
idade e só remove com `--confirmar`. O original só sai da pasta de entrada
depois que o PDF/A foi gerado *e* verificado — e vai para `Processados`.

### Saída atômica

A conversão escreve num arquivo temporário e só então renomeia para o destino.
Interromper o processo no meio nunca deixa um PDF truncado na pasta de saída — o
modo de falha mais traiçoeiro num acervo, porque o arquivo parece pronto.

### Paralelismo em duas dimensões

O laço `for` do `cmd` processava um arquivo por vez. Lançar N conversões também
seria errado, porque o OCRmyPDF já paraleliza páginas internamente e as duas
camadas competiriam pela mesma CPU. O Scriptor decide quantos documentos
simultâneos **e** quantos núcleos por documento, e ordena o lote do maior para o
menor para reduzir o tempo total.

### Idempotência e auditoria

Um ledger SQLite guarda o SHA-256 de cada origem, o hash da receita (idioma,
perfil, otimização, versões de Tesseract e Ghostscript), a estratégia usada, o
resultado da verificação e o SHA-256 da saída. Reexecutar sobre a mesma pasta não
reprocessa nada. Atualizar o Tesseract muda a receita e reprocessa o que for
afetado. As linhas nunca são sobrescritas.

### Descoberta de ferramentas

As duas apostilas de troubleshooting do kit — PATH do Tesseract e instalação do
idioma português — existiam para contornar suposições do script. Ambas ficaram
obsoletas: os binários são localizados no PATH, no **registro do Windows** e nos
diretórios de instalação; idiomas ausentes são instalados a partir do kit, e se
`Program Files` for somente leitura, um espelho de `tessdata` é montado no
perfil do usuário com hardlinks.

---

## Estrutura

```
Scriptor.cmd            ponto de entrada — duplo clique
Documentos/             workspace do operador
  scriptor.toml         configuração, comentada e editável à mão
  Entrada/              documentos a processar
  Saida/                PDF/A gerados (hierarquia da entrada preservada)
  Processados/          originais convertidos com sucesso
  Falhas/               originais que não puderam ser convertidos
  _scriptor/
    ledger.sqlite3      trilha de auditoria
    logs/               log por documento, com o comando de cada tentativa
    relatorios/         relatório JSON por execução
src/scriptor/           código
  analysis.py           perfilagem do documento
  strategy.py           escolha de estratégia e escada de recuperação
  runner.py             conversão isolada, atômica, com limite de tempo
  pipeline.py           orquestração e paralelismo
  conformance.py        verificação PDF/A
  ledger.py             idempotência e auditoria
  toolchain.py          descoberta de Tesseract e Ghostscript
  web/                  interface do operador
tests/                  92 testes, incluindo conversões reais
instaladores/           Python, Tesseract e Ghostscript (não versionado)
idiomas/                por.traineddata (não versionado)
legado/                 o kit .bat original, preservado como referência
```

## Códigos de saída

| Código | Significado |
| --- | --- |
| 0 | Tudo convertido. |
| 1 | Ao menos um documento falhou ou foi recusado. |
| 2 | Nada a fazer. |
| 3 | Erro de configuração ou de ambiente. |
| 130 | Interrompido pelo operador. |

## Decisões deliberadas

**`--jbig2-lossy` não é usado.** A compressão JBIG2 com perda substitui glifos
visualmente parecidos por um símbolo comum — o defeito que trocou dígitos em
digitalizações da Xerox. Inaceitável em acervo, por menor que fosse o arquivo.

**`optimize` padrão é 1.** Os níveis 2 e 3 recomprimem imagem com perda.
Disponíveis, nunca automáticos.

**`deskew`, `rotate_pages` e `clean` vêm desligados.** Todos alteram a imagem da
página. Melhoram o reconhecimento em digitalizações ruins e degradam o documento
quando erram; a escolha é do operador, não do padrão.

**A conversão roda em subprocesso.** O OCRmyPDF é importável como biblioteca,
mas invoca Tesseract e Ghostscript — binários nativos que, diante de um PDF
malformado, podem travar ou abortar. Num lote de milhares, um documento não pode
derrubar o restante.

**A interface é servida localmente, não é uma janela nativa.** Tkinter não
alcança a barra visual do projeto e PySide6 acrescentaria mais de cem megabytes
ao kit. O servidor escuta apenas em `127.0.0.1`, exige um cabeçalho de
autenticação em toda chamada de API — o que impede acionamento por qualquer
página de terceiros — valida o cabeçalho `Host` contra DNS rebinding e nunca
aceita caminho de arquivo vindo do cliente.

**O ambiente Python fica em `%LOCALAPPDATA%`, não na pasta do kit.** Um
ambiente virtual tem milhares de arquivos pequenos; sincronizar isso pelo
OneDrive inutiliza a máquina.

## Desenvolvimento

```powershell
pip install -e ".[dev]"
pytest                 # tudo
pytest -m "not slow"   # sem as conversões reais
```
