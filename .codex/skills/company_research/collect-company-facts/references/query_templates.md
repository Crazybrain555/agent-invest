# Query templates for collect-company-facts

## News (gdelt.search_articles)
- Base: "{ticker}" OR "{company_name}"
- With disambiguation: "{ticker}" OR "{company_name}" AND (earnings OR acquisition OR product OR lawsuit)
- Example: "AAPL" OR "Apple Inc" AND (earnings OR guidance OR product)

## Papers (openalex.search_works)
- Base: "{company_name}" OR "{ticker}"
- Add domain: "{company_name}" AND (semiconductor OR drug OR materials OR software)
- Example: "NVIDIA" AND (GPU OR CUDA OR accelerator)

## arXiv (mcp__arxiv__search_papers)
- abs:"{company_name}" OR "{ticker}" AND (model OR algorithm OR accelerator)
- Example: abs:"DeepMind" AND (reinforcement learning OR agent)

## PubMed (mcp__pubmed__search_pubmed_advanced)
- term: "{company_name}" AND (drug OR trial OR efficacy)
- Example: "Moderna" AND (mRNA OR vaccine)
