# Azure AI Search — Index, Skillset, Indexer Setup

Use this document to recreate the Azure AI Search resources in a new Azure subscription.
All JSON bodies are ready to paste directly into the Azure Portal REST console or `curl`.

---

## Prerequisites

| Resource | Purpose |
|---|---|
| Azure AI Search service | Hosts the index, skillset, indexer |
| Azure Blob Storage container | Stores the source PDF manuals |
| Azure OpenAI service | `text-embedding-ada-002` deployment for chunk embeddings |
| Azure AI Services (Cognitive Services) | Required by the skillset for OCR and Document Intelligence |

**Known values for this deployment:**

```
SEARCH_ENDPOINT   = https://<your-search-resource>.search.azure.us
SEARCH_API_KEY    = <your-search-admin-key>
STORAGE_CONN_STR  = DefaultEndpointsProtocol=https;AccountName=<acct>;AccountKey=<key>;...
STORAGE_CONTAINER = containerpsegtmandev01
AOAI_ENDPOINT     = https://azopenai-pseg-tman-dev01.openai.azure.us/
AOAI_API_KEY      = <your-openai-key>
AOAI_EMBED_DEPLOY = textembeddingadapsegtm
AI_SERVICES_KEY   = <your-ai-services-key>
```

---

## Step 1 — Data Source

**REST endpoint:** `PUT {SEARCH_ENDPOINT}/datasources/pseg-techm-blob-ds-final?api-version=2024-05-01-preview`

```json
{
  "name": "pseg-techm-blob-ds-final",
  "type": "azureblob",
  "credentials": {
    "connectionString": "<STORAGE_CONN_STR>"
  },
  "container": {
    "name": "containerpsegtmandev01",
    "query": ""
  },
  "dataChangeDetectionPolicy": {
    "@odata.type": "#Microsoft.Azure.Search.HighWaterMarkChangeDetectionPolicy",
    "highWaterMarkColumnName": "metadata_storage_last_modified"
  },
  "dataDeletionDetectionPolicy": {
    "@odata.type": "#Microsoft.Azure.Search.NativeBlobSoftDeleteDeletionDetectionPolicy"
  }
}
```

---

## Step 2 — Index Schema

**REST endpoint:** `PUT {SEARCH_ENDPOINT}/indexes/rag-psegtechm-index-final?api-version=2024-05-01-preview`

> **Important:** `chunk_id` is the document key. `text_vector` must match the
> dimensions of your embedding model: `1536` for `text-embedding-ada-002`.

```json
{
  "name": "rag-psegtechm-index-final",
  "fields": [
    {
      "name": "chunk_id",
      "type": "Edm.String",
      "key": true,
      "searchable": true,
      "filterable": true,
      "retrievable": true,
      "sortable": true,
      "facetable": false,
      "analyzer": "keyword"
    },
    {
      "name": "parent_id",
      "type": "Edm.String",
      "searchable": false,
      "filterable": true,
      "retrievable": true,
      "sortable": false,
      "facetable": false
    },
    {
      "name": "title",
      "type": "Edm.String",
      "searchable": true,
      "filterable": true,
      "retrievable": true,
      "sortable": true,
      "facetable": false
    },
    {
      "name": "source_file",
      "type": "Edm.String",
      "searchable": true,
      "filterable": true,
      "retrievable": true,
      "sortable": true,
      "facetable": true
    },
    {
      "name": "source_url",
      "type": "Edm.String",
      "searchable": false,
      "filterable": false,
      "retrievable": true,
      "sortable": false,
      "facetable": false
    },
    {
      "name": "header_1",
      "type": "Edm.String",
      "searchable": true,
      "filterable": false,
      "retrievable": true,
      "sortable": false,
      "facetable": false
    },
    {
      "name": "header_2",
      "type": "Edm.String",
      "searchable": true,
      "filterable": false,
      "retrievable": true,
      "sortable": false,
      "facetable": false
    },
    {
      "name": "header_3",
      "type": "Edm.String",
      "searchable": true,
      "filterable": false,
      "retrievable": true,
      "sortable": false,
      "facetable": false
    },
    {
      "name": "layout_ordinal",
      "type": "Edm.Int32",
      "searchable": false,
      "filterable": true,
      "retrievable": true,
      "sortable": true,
      "facetable": false
    },
    {
      "name": "chunk",
      "type": "Edm.String",
      "searchable": true,
      "filterable": false,
      "retrievable": true,
      "sortable": false,
      "facetable": false
    },
    {
      "name": "chunk_for_semantic",
      "type": "Edm.String",
      "searchable": true,
      "filterable": false,
      "retrievable": true,
      "sortable": false,
      "facetable": false
    },
    {
      "name": "ocr_fallback_text",
      "type": "Edm.String",
      "searchable": true,
      "filterable": false,
      "retrievable": true,
      "sortable": false,
      "facetable": false
    },
    {
      "name": "text_vector",
      "type": "Collection(Edm.Single)",
      "searchable": true,
      "filterable": false,
      "retrievable": false,
      "stored": false,
      "sortable": false,
      "facetable": false,
      "dimensions": 1536,
      "vectorSearchProfile": "pseg-hnsw-profile"
    }
  ],
  "similarity": {
    "@odata.type": "#Microsoft.Azure.Search.BM25Similarity"
  },
  "semantic": {
    "defaultConfiguration": "manual-semantic-config",
    "configurations": [
      {
        "name": "manual-semantic-config",
        "prioritizedFields": {
          "titleField": {
            "fieldName": "title"
          },
          "prioritizedContentFields": [
            {
              "fieldName": "chunk_for_semantic"
            },
            {
              "fieldName": "chunk"
            },
            {
              "fieldName": "ocr_fallback_text"
            }
          ],
          "prioritizedKeywordsFields": [
            {
              "fieldName": "source_file"
            },
            {
              "fieldName": "header_1"
            },
            {
              "fieldName": "header_2"
            },
            {
              "fieldName": "header_3"
            }
          ]
        }
      }
    ]
  },
  "vectorSearch": {
    "algorithms": [
      {
        "name": "pseg-hnsw-algo",
        "kind": "hnsw",
        "hnswParameters": {
          "m": 8,
          "efConstruction": 400,
          "efSearch": 500,
          "metric": "cosine"
        }
      }
    ],
    "profiles": [
      {
        "name": "pseg-hnsw-profile",
        "algorithm": "pseg-hnsw-algo"
      }
    ],
    "vectorizers": [],
    "compressions": []
  }
}
```

---

## Step 3 — Skillset

**REST endpoint:** `PUT {SEARCH_ENDPOINT}/skillsets/pseg-techm-skillset-final?api-version=2024-05-01-preview`

The pipeline:
1. **Document Intelligence Layout** — extracts structured markdown sections (h1/h2/h3 headings, paragraphs, tables) from native-text and image PDFs
2. **OCR** — extracts text from scanned/image pages as a fallback field
3. **Merge** — merges native document text with OCR image text into a single fallback field (`ocrMergedContent`)
4. **Split** — chunks each layout section into 1 200-character pages with 200-char overlap
5. **Embedding** — calls Azure OpenAI to vectorise each chunk

```json
{
  "name": "pseg-techm-skillset-final",
  "description": "Final combined layout-first + OCR-fallback skillset for high-precision technical manual retrieval",
  "skills": [
    {
      "@odata.type": "#Microsoft.Skills.Util.DocumentIntelligenceLayoutSkill",
      "name": "layout-skill",
      "description": "Extract structured markdown sections from the document",
      "context": "/document",
      "outputMode": "oneToMany",
      "markdownHeaderDepth": "h3",
      "inputs": [
        {
          "name": "file_data",
          "source": "/document/file_data"
        }
      ],
      "outputs": [
        {
          "name": "markdown_document",
          "targetName": "markdownDocument"
        }
      ]
    },
    {
      "@odata.type": "#Microsoft.Skills.Vision.OcrSkill",
      "name": "ocr-skill",
      "description": "Extract text from normalized images and embedded PDF images",
      "context": "/document/normalized_images/*",
      "defaultLanguageCode": "en",
      "detectOrientation": true,
      "inputs": [
        {
          "name": "image",
          "source": "/document/normalized_images/*"
        }
      ],
      "outputs": [
        {
          "name": "text",
          "targetName": "text"
        }
      ]
    },
    {
      "@odata.type": "#Microsoft.Skills.Text.MergeSkill",
      "name": "merge-native-and-ocr",
      "description": "Merge native extracted text with OCR text into a document-level fallback field",
      "context": "/document",
      "insertPreTag": " ",
      "insertPostTag": " ",
      "inputs": [
        {
          "name": "text",
          "source": "/document/content"
        },
        {
          "name": "itemsToInsert",
          "source": "/document/normalized_images/*/text"
        },
        {
          "name": "offsets",
          "source": "/document/normalized_images/*/contentOffset"
        }
      ],
      "outputs": [
        {
          "name": "mergedText",
          "targetName": "ocrMergedContent"
        }
      ]
    },
    {
      "@odata.type": "#Microsoft.Skills.Text.SplitSkill",
      "name": "split-markdown-sections",
      "description": "Split each structured section into overlapping chunks",
      "context": "/document/markdownDocument/*",
      "defaultLanguageCode": "en",
      "textSplitMode": "pages",
      "maximumPageLength": 1200,
      "pageOverlapLength": 200,
      "unit": "characters",
      "inputs": [
        {
          "name": "text",
          "source": "/document/markdownDocument/*/content"
        }
      ],
      "outputs": [
        {
          "name": "textItems",
          "targetName": "pages"
        }
      ]
    },
    {
      "@odata.type": "#Microsoft.Skills.Text.AzureOpenAIEmbeddingSkill",
      "name": "embed-chunks",
      "description": "Generate Ada 002 embeddings for each chunk",
      "context": "/document/markdownDocument/*/pages/*",
      "resourceUri": "https://azopenai-pseg-tman-dev01.openai.azure.us/",
      "apiKey": "<AOAI_API_KEY>",
      "deploymentId": "textembeddingadapsegtm",
      "modelName": "text-embedding-ada-002",
      "dimensions": 1536,
      "inputs": [
        {
          "name": "text",
          "source": "/document/markdownDocument/*/pages/*"
        }
      ],
      "outputs": [
        {
          "name": "embedding",
          "targetName": "text_vector"
        }
      ]
    }
  ],
  "cognitiveServices": {
    "@odata.type": "#Microsoft.Azure.Search.CognitiveServicesByKey",
    "key": "<AI_SERVICES_KEY>"
  },
  "indexProjections": {
    "selectors": [
      {
        "targetIndexName": "rag-psegtechm-index-final",
        "parentKeyFieldName": "parent_id",
        "sourceContext": "/document/markdownDocument/*/pages/*",
        "mappings": [
          {
            "name": "title",
            "source": "/document/metadata_storage_name"
          },
          {
            "name": "source_file",
            "source": "/document/metadata_storage_name"
          },
          {
            "name": "source_url",
            "source": "/document/metadata_storage_path"
          },
          {
            "name": "header_1",
            "source": "/document/markdownDocument/*/sections/h1"
          },
          {
            "name": "header_2",
            "source": "/document/markdownDocument/*/sections/h2"
          },
          {
            "name": "header_3",
            "source": "/document/markdownDocument/*/sections/h3"
          },
          {
            "name": "layout_ordinal",
            "source": "/document/markdownDocument/*/ordinal_position"
          },
          {
            "name": "chunk",
            "source": "/document/markdownDocument/*/pages/*"
          },
          {
            "name": "chunk_for_semantic",
            "source": "/document/markdownDocument/*/pages/*"
          },
          {
            "name": "ocr_fallback_text",
            "source": "/document/ocrMergedContent"
          },
          {
            "name": "text_vector",
            "source": "/document/markdownDocument/*/pages/*/text_vector"
          }
        ]
      }
    ],
    "parameters": {
      "projectionMode": "skipIndexingParentDocuments"
    }
  }
}
```

---

## Step 4 — Indexer

**REST endpoint:** `PUT {SEARCH_ENDPOINT}/indexers/pseg-techm-indexer-final?api-version=2024-05-01-preview`

```json
{
  "name": "pseg-techm-indexer-final",
  "dataSourceName": "pseg-techm-blob-ds-final",
  "targetIndexName": "rag-psegtechm-index-final",
  "skillsetName": "pseg-techm-skillset-final",
  "parameters": {
    "batchSize": 1,
    "maxFailedItems": -1,
    "maxFailedItemsPerBatch": -1,
    "configuration": {
      "dataToExtract": "contentAndMetadata",
      "parsingMode": "default",
      "allowSkillsetToReadFileData": true,
      "imageAction": "generateNormalizedImages",
      "failOnUnsupportedContentType": false,
      "failOnUnprocessableDocument": false
    }
  },
  "fieldMappings": [],
  "outputFieldMappings": []
}
```

---

## Step 5 — Run the Indexer

After creating all four resources, trigger an immediate run:

**REST endpoint:** `POST {SEARCH_ENDPOINT}/indexers/pseg-techm-indexer-final/run?api-version=2024-05-01-preview`

Check status:

**REST endpoint:** `GET {SEARCH_ENDPOINT}/indexers/pseg-techm-indexer-final/status?api-version=2024-05-01-preview`

---

## .env Values After Setup

Once indexing completes, set these in your `.env`:

```env
AZURE_SEARCH_ENDPOINT=https://<your-search-resource>.search.azure.us
AZURE_SEARCH_API_KEY=<your-search-admin-key>
AZURE_SEARCH_INDEX=rag-psegtechm-index-final

# Field mappings — must match the index schema exactly
SEARCH_CONTENT_FIELD=chunk
SEARCH_VECTOR_FIELD=text_vector
SEARCH_FILENAME_FIELD=source_file
SEARCH_PAGE_FIELD=layout_ordinal
SEARCH_CHUNK_ID_FIELD=chunk_id
SEARCH_URL_FIELD=source_url
SEARCH_SECTION_FIELD=header_1

# Semantic reranker — enabled because the index has manual-semantic-config
USE_SEMANTIC_RERANKER=true
SEMANTIC_CONFIG_NAME=manual-semantic-config
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `text_vector` is empty / all zeros | Embedding skill misconfigured | Check `AOAI_ENDPOINT` and `AOAI_API_KEY` in skillset |
| `chunk` is always null | Layout skill output path mismatch | Verify `markdownDocument/*/content` path in SplitSkill input |
| `header_1/2/3` always null | Section path wrong | Try `/document/markdownDocument/*/sections/h1` — check DocumentIntelligenceLayoutSkill output schema for your API version |
| `layout_ordinal` always null | Ordinal path wrong | Try `/document/markdownDocument/*/ordinal_position` — field name may vary by API version |
| All queries return gate-rejected responses | Scores too low | Lower `MIN_AVG_SCORE` in `.env` — RRF hybrid scores range 0.01–0.033 |
| `chunk_id` key conflicts during reindex | Duplicate document IDs | Confirm `projectionMode: "skipIndexingParentDocuments"` is set in skillset indexProjections |
| Indexer fails on large PDFs | Timeout / batch too large | Reduce `batchSize` to 1 (already set), increase `maxFailedItems` |
| OCR text not appearing in `ocr_fallback_text` | MergeSkill or imageAction missing | Confirm `imageAction: "generateNormalizedImages"` in indexer config |
