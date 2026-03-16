{
  "name": "pseg-techm-skillset-vnext",
  "description": "Gov-safer skillset for technical manuals using Document Intelligence layout, OCR, and ada-002 embeddings",
  "skills": [
    {
      "@odata.type": "#Microsoft.Skills.Util.DocumentIntelligenceLayoutSkill",
      "name": "document-layout-skill",
      "description": "Document Intelligence layout extraction with text chunks and location metadata",
      "context": "/document",
      "outputMode": "oneToMany",
      "outputFormat": "text",
      "extractionOptions": [
        "locationMetadata"
      ],
      "chunkingProperties": {
        "unit": "characters",
        "maximumLength": 2000,
        "overlapLength": 200
      },
      "inputs": [
        {
          "name": "file_data",
          "source": "/document/file_data"
        }
      ],
      "outputs": [
        {
          "name": "text_sections",
          "targetName": "text_sections"
        }
      ]
    },
    {
      "@odata.type": "#Microsoft.Skills.Vision.OcrSkill",
      "name": "ocr-skill",
      "description": "OCR over normalized images for labels, warnings, callouts, and scanned text",
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
          "targetName": "ocrText"
        }
      ]
    },
    {
      "@odata.type": "#Microsoft.Skills.Text.MergeSkill",
      "name": "merge-ocr-into-text-skill",
      "description": "Merge OCR text into document-level fallback text",
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
          "source": "/document/normalized_images/*/ocrText"
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
      "@odata.type": "#Microsoft.Skills.Text.AzureOpenAIEmbeddingSkill",
      "name": "text-embedding-skill",
      "description": "Embeddings for text chunks",
      "context": "/document/text_sections/*",
      "resourceUri": "https://azopenai-pseg-tman-dev01.openai.azure.us/",
      "apiKey": "YOUR_AOAI_KEY",
      "deploymentId": "textembeddingadapsegtm",
      "modelName": "text-embedding-ada-002",
      "dimensions": 1536,
      "inputs": [
        {
          "name": "text",
          "source": "/document/text_sections/*/content"
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
    "@odata.type": "#Microsoft.Azure.Search.AIServicesByKey",
    "subdomainUrl": "https://aiservicesforocr.cognitiveservices.azure.us/",
    "key": "YOUR_AI_SERVICES_KEY"
  },
  "indexProjections": {
    "selectors": [
      {
        "targetIndexName": "rag-psegtechm-index-vnext",
        "parentKeyFieldName": "text_document_id",
        "sourceContext": "/document/text_sections/*",
        "mappings": [
          {
            "name": "record_type",
            "source": "='text'"
          },
          {
            "name": "content_embedding",
            "source": "/document/text_sections/*/text_vector"
          },
          {
            "name": "content_text",
            "source": "/document/text_sections/*/content"
          },
          {
            "name": "page_number",
            "source": "/document/text_sections/*/locationMetadata/pageNumber"
          },
          {
            "name": "layout_ordinal",
            "source": "/document/text_sections/*/locationMetadata/ordinalPosition"
          },
          {
            "name": "bounding_polygons",
            "source": "/document/text_sections/*/locationMetadata/boundingPolygons"
          },
          {
            "name": "document_title",
            "source": "/document/metadata_storage_name"
          },
          {
            "name": "source_file",
            "source": "/document/metadata_storage_name"
          },
          {
            "name": "source_url",
            "source": "/document/metadata_storage_path"
          }
        ]
      }
    ],
    "parameters": {
      "projectionMode": "skipIndexingParentDocuments"
    }
  }
}
