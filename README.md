# Radar de Dividendos — dados compartilhados e cache

O repositório contém uma interface estática (`index.html`) e uma rotina agendada (`coleta.py`). O workflow do GitHub Actions atualiza o snapshot nos dias úteis, às 18h de Brasília. O app continua funcionando sem o snapshot; quando `dados.json` está disponível, carrega-o como cache público de catálogo e preços informativos.

## Como o cache funciona

`dados.json` é um snapshot compartilhado e versionado no próprio repositório. A rotina faz upsert por ticker: não apaga o último preço/fundamento/provento válido quando uma fonte falha, responde vazio ou apresenta conflito. Quando a enumeração do catálogo foi confirmada como completa, ativos ausentes deixam de ser marcados como atuais, mas permanecem no histórico com seus dados anteriores. O histórico de preço no JSON guarda até 30 observações por ticker, uma por data, e substitui a observação do mesmo dia em vez de duplicá-la.

A lista primária usa a API pública paginada da brapi (`stock`, `fund` e `bdr`), igual à fonte de catálogo que o front-end já consultava. Se a consulta completa da brapi falhar, o coletor tenta o diretório CotaLab, mas marca essa coleta como incompleta e não conclui que os símbolos que faltaram foram removidos. Uma enumeração completa do mesmo dia é reutilizada; tickers com cotação já validada no mesmo dia e proventos já consultados no mesmo dia não repetem essas consultas.

## Validação e proveniência

Uma cotação nova só é promovida ao snapshot se houver preço brapi e fechamento diário do Yahoo Finance e a diferença relativa for de até 8%. Acima disso, o snapshot registra os dois candidatos como conflito e conserva o último preço validado. Se só uma fonte responder, o preço não substitui o último valor válido. A data de observação é a data da série Yahoo; o horário de verificação é separado do `requestedAt` do catálogo, que não é tratado como horário exato da cotação.

Fundamentos do coletor vêm do Fundamentus e proventos históricos do Yahoo Finance; são campos de fonte única, identificados como tal. A interface **não** usa os fundamentos/proventos desse cache para gerar sinal de compra ou venda. As recomendações existentes continuam condicionadas aos dados detalhados próprios do Radar (brapi e Dados B3), com suas validações e controles atuais. Preços do snapshot aparecem como informativos, não autorizam uma recomendação.

## Executar e testar

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
python -u coleta.py
```

O workflow agendado executa a suíte de testes antes da coleta e publica somente `dados.json` e `catalogo_ativos.json` quando há alteração. Para evitar apagar o estado conhecido, o coletor interrompe a escrita se o `dados.json` anterior estiver inválido. Nenhuma chave de API deve ser gravada nesses arquivos nem no HTML.

## Limites importantes

Este projeto não conecta a um banco de dados externo: o JSON e o histórico Git são o armazenamento desta primeira etapa. O arquivo é público neste repositório e no GitHub Pages; não inclua carteira, tokens, dados pessoais ou informações privadas. A disponibilidade, cobertura, atraso, formato e condições de uso dos provedores públicos podem mudar. CotaLab, Fundamentus e yfinance não são fontes oficiais da B3; o catálogo brapi também deve ser tratado como uma fonte de dados de terceiros, não como cadastro legal da bolsa. O cache não promete cobertura de preço/fundamentos para cada ticker, não contorna limites dos provedores e não substitui a conferência das fontes originais.
