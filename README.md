# Radar B3

Este repositório contém apenas uma interface pública de referência inspirada no layout antigo do Radar B3. A página não consulta provedores, não carrega arquivos de dados, não calcula sinais e não deve ser usada como análise financeira.

## Beta privado

O aplicativo funcional está sendo preparado separadamente como um Beta privado, com autenticação do proprietário e backend. As chaves dos provedores permanecem como secrets server-side; nunca devem ser incluídas no navegador, URLs, logs, arquivos versionados ou no GitHub.

A arquitetura planejada usa a BrAPI para cotações e histórico, Dados B3 para os indicadores/múltiplos/proventos cobertos pelo serviço e dados oficiais da CVM para demonstrações financeiras. O plano gratuito Dados B3 limita o uso pessoal a 200 chamadas por dia e não autoriza redistribuir ou publicar os dados. O sistema deverá enfileirar chamadas, respeitar limites e apresentar dados ausentes/desatualizados como indisponíveis, sem preencher lacunas com números fictícios.

Não usar raspagem do StatusInvest, nem publicar snapshots, caches ou respostas financeiras de provedores neste repositório ou no GitHub Pages. Não executar ingestão real em build ou teste.

## Histórico Git

Esta limpeza remove os arquivos de coleta e snapshots da árvore atual e interrompe novas execuções do workflow antigo. Ela não reescreve o histórico Git nem apaga cópias que já tenham sido publicadas em commits anteriores. Para preservar o histórico, não se fará force-push.
