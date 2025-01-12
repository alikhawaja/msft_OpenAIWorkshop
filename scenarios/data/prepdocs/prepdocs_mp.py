import argparse
import base64
import glob
import html
import io
import os
import re
import time
import tempfile
import re  
from concurrent.futures import ProcessPoolExecutor  

from openai import AzureOpenAI
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
from azure.identity import AzureDeveloperCliCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import *
from azure.storage.blob import BlobServiceClient
from pypdf import PdfReader, PdfWriter
from tenacity import retry, stop_after_attempt, wait_random_exponential
import json
MAX_SECTION_LENGTH = 1500
SENTENCE_SEARCH_LIMIT = 100
SECTION_OVERLAP = 200
def blob_name_from_file_page(filename, page = 0):
    if os.path.splitext(filename)[1].lower() == ".pdf":
        return os.path.splitext(os.path.basename(filename))[0] + f"-{page}" + ".pdf"
    else:
        return os.path.basename(filename)
def list_blobs_recursively(storage_creds, args):  
    blob_list_file = 'blob_list.txt'  
    if os.path.isfile(blob_list_file):  
        with open(blob_list_file, 'r', encoding="utf8") as file:  
            blob_names = file.read().splitlines()  
    else:  
        blob_service = BlobServiceClient(account_url=f"https://{args.storageaccount}.blob.core.windows.net", credential=storage_creds)  
        blob_container = blob_service.get_container_client(args.container_source)  
        blobs = blob_container.list_blobs(name_starts_with=None)  
        blob_names = [blob.name for blob in blobs]  
          
        # Save the blob names to blob_list.txt  
        with open(blob_list_file, 'w') as file:  
            for blob_name in blob_names:  
                file.write(f"{blob_name}\n")  
  
    return blob_names  

def upload_blobs(filename):
    blob_service = BlobServiceClient(account_url=f"https://{args.storageaccount}.blob.core.windows.net", credential=storage_creds)
    blob_container = blob_service.get_container_client(args.container)
    if not blob_container.exists():
        blob_container.create_container()

    # if file is PDF split into pages and upload each page as a separate blob
    if os.path.splitext(filename)[1].lower() == ".pdf":
        reader = PdfReader(filename)
        pages = reader.pages
        for i in range(len(pages)):
            blob_name = blob_name_from_file_page(filename, i)
            # if args.verbose: print(f"\tUploading blob for page {i} -> {blob_name}")
            f = io.BytesIO()
            writer = PdfWriter()
            writer.add_page(pages[i])
            writer.write(f)
            f.seek(0)
            blob_container.upload_blob(blob_name, f, overwrite=True)
    else:
        blob_name = blob_name_from_file_page(filename)
        with open(filename,"rb") as data:
            blob_container.upload_blob(blob_name, data, overwrite=True)

def remove_blobs(filename):
    if args.verbose: print(f"Removing blobs for '{filename or '<all>'}'")
    blob_service = BlobServiceClient(account_url=f"https://{args.storageaccount}.blob.core.windows.net", credential=storage_creds)
    blob_container = blob_service.get_container_client(args.container)
    if blob_container.exists():
        if filename == None:
            blobs = blob_container.list_blob_names()
        else:
            prefix = os.path.splitext(os.path.basename(filename))[0]
            blobs = filter(lambda b: re.match(f"{prefix}-\d+\.pdf", b), blob_container.list_blob_names(name_starts_with=os.path.splitext(os.path.basename(prefix))[0]))
        for b in blobs:
            if args.verbose: print(f"\tRemoving blob {b}")
            blob_container.delete_blob(b)

def table_to_html(table):
    table_html = "<table>"
    rows = [sorted([cell for cell in table.cells if cell.row_index == i], key=lambda cell: cell.column_index) for i in range(table.row_count)]
    for row_cells in rows:
        table_html += "<tr>"
        for cell in row_cells:
            tag = "th" if (cell.kind == "columnHeader" or cell.kind == "rowHeader") else "td"
            cell_spans = ""
            if cell.column_span > 1: cell_spans += f" colSpan={cell.column_span}"
            if cell.row_span > 1: cell_spans += f" rowSpan={cell.row_span}"
            table_html += f"<{tag}{cell_spans}>{html.escape(cell.content)}</{tag}>"
        table_html +="</tr>"
    table_html += "</table>"
    return table_html

def get_document_text(filename):
    offset = 0
    page_map = []
    if args.localpdfparser:
        reader = PdfReader(filename)
        pages = reader.pages
        for page_num, p in enumerate(pages):
            page_text = p.extract_text()
            page_map.append((page_num, offset, page_text))
            offset += len(page_text)
    else:
        # if args.verbose: print(f"Extracting text from '{filename}' using Azure Form Recognizer")
        form_recognizer_client = DocumentAnalysisClient(endpoint=f"https://{args.formrecognizerservice}.cognitiveservices.azure.com/", credential=formrecognizer_creds, headers={"x-ms-useragent": "azure-search-chat-demo/1.0.0"})
        with open(filename, "rb") as f:
            poller = form_recognizer_client.begin_analyze_document("prebuilt-layout", document = f)
        form_recognizer_results = poller.result()

        for page_num, page in enumerate(form_recognizer_results.pages):
            tables_on_page = [table for table in form_recognizer_results.tables if table.bounding_regions[0].page_number == page_num + 1]

            # mark all positions of the table spans in the page
            page_offset = page.spans[0].offset
            page_length = page.spans[0].length
            table_chars = [-1]*page_length
            for table_id, table in enumerate(tables_on_page):
                for span in table.spans:
                    # replace all table spans with "table_id" in table_chars array
                    for i in range(span.length):
                        idx = span.offset - page_offset + i
                        if idx >=0 and idx < page_length:
                            table_chars[idx] = table_id

            # build page text by replacing charcters in table spans with table html
            page_text = ""
            added_tables = set()
            for idx, table_id in enumerate(table_chars):
                if table_id == -1:
                    page_text += form_recognizer_results.content[page_offset + idx]
                elif not table_id in added_tables:
                    page_text += table_to_html(tables_on_page[table_id])
                    added_tables.add(table_id)

            page_text += " "
            page_map.append((page_num, offset, page_text))
            offset += len(page_text)

    return page_map

def split_text(page_map):
    SENTENCE_ENDINGS = [".", "!", "?"]
    WORDS_BREAKS = [",", ";", ":", " ", "(", ")", "[", "]", "{", "}", "\t", "\n"]
    # if args.verbose: print(f"Splitting '{filename}' into sections")

    def find_page(offset):
        l = len(page_map)
        for i in range(l - 1):
            if offset >= page_map[i][1] and offset < page_map[i + 1][1]:
                return i
        return l - 1

    all_text = "".join(p[2] for p in page_map)
    length = len(all_text)
    start = 0
    end = length
    while start + SECTION_OVERLAP < length:
        last_word = -1
        end = start + MAX_SECTION_LENGTH

        if end > length:
            end = length
        else:
            # Try to find the end of the sentence
            while end < length and (end - start - MAX_SECTION_LENGTH) < SENTENCE_SEARCH_LIMIT and all_text[end] not in SENTENCE_ENDINGS:
                if all_text[end] in WORDS_BREAKS:
                    last_word = end
                end += 1
            if end < length and all_text[end] not in SENTENCE_ENDINGS and last_word > 0:
                end = last_word # Fall back to at least keeping a whole word
        if end < length:
            end += 1

        # Try to find the start of the sentence or at least a whole word boundary
        last_word = -1
        while start > 0 and start > end - MAX_SECTION_LENGTH - 2 * SENTENCE_SEARCH_LIMIT and all_text[start] not in SENTENCE_ENDINGS:
            if all_text[start] in WORDS_BREAKS:
                last_word = start
            start -= 1
        if all_text[start] not in SENTENCE_ENDINGS and last_word > 0:
            start = last_word
        if start > 0:
            start += 1

        section_text = all_text[start:end]
        yield (section_text, find_page(start))

        last_table_start = section_text.rfind("<table")
        if (last_table_start > 2 * SENTENCE_SEARCH_LIMIT and last_table_start > section_text.rfind("</table")):
            # If the section ends with an unclosed table, we need to start the next section with the table.
            # If table starts inside SENTENCE_SEARCH_LIMIT, we ignore it, as that will cause an infinite loop for tables longer than MAX_SECTION_LENGTH
            # If last table starts inside SECTION_OVERLAP, keep overlapping
            # if args.verbose: print(f"Section ends with unclosed table, starting next section with the table at page {find_page(start)} offset {start} table start {last_table_start}")
            start = min(end - SECTION_OVERLAP, start + last_table_start)
        else:
            start = end - SECTION_OVERLAP
        
    if start + SECTION_OVERLAP < end:
        yield (all_text[start:end], find_page(start))

def filename_to_id(filename):
    filename_ascii = re.sub("[^0-9a-zA-Z_-]", "_", filename)
    filename_hash = base64.b16encode(filename.encode('utf-8')).decode('ascii')
    return f"file-{filename_ascii}-{filename_hash}"

def create_sections(filename, page_map, use_vectors):
    @retry(wait=wait_random_exponential(min=1, max=60), stop=stop_after_attempt(15), before_sleep=before_retry_sleep)
    def compute_embedding(text):
        # print("args.openaideployment", args.openaideployment)
        text = text.replace("\n", " ")
        return client.embeddings.create(input = [text], model=args.openaideployment).data[0].embedding

    #product = product_file_map[filename]
    
    file_id = filename_to_id(filename)
    for i, (content, pagenum) in enumerate(split_text(page_map)):
        section = {
            "id": f"{file_id}-page-{i}",
            "content": content,
            #"product": product,
            "sourcepage": blob_name_from_file_page(filename, pagenum),
            "sourcefile": filename
        }
        if use_vectors:
            section["embedding"] = compute_embedding(content)
        yield section

def before_retry_sleep(retry_state):
    if args.verbose: print(f"Rate limited on the OpenAI embeddings API, sleeping before retrying...")


def create_search_index():
    if args.verbose: print(f"Ensuring search index {args.index} exists")
    index_client = SearchIndexClient(endpoint=f"https://{args.searchservice}.search.windows.net/",
                                     credential=search_creds)
    if args.index not in index_client.list_index_names():
        index = SearchIndex(
            name=args.index,

            fields=[
                SimpleField(name="id", type=SearchFieldDataType.String, key=True),
                SearchableField(name="content", type=SearchFieldDataType.String),
                SearchField(name="embedding", type=SearchFieldDataType.Collection(SearchFieldDataType.Single), 
                            hidden=False, searchable=True, filterable=False, sortable=False, facetable=False,
                            vector_search_dimensions=1536, vector_search_profile_name="myHnswProfile"),
                SimpleField(name="product", type=SearchFieldDataType.String, filterable=True, facetable=True),
                SimpleField(name="sourcepage", type=SearchFieldDataType.String, filterable=True, facetable=True),
                SimpleField(name="sourcefile", type=SearchFieldDataType.String, filterable=True, facetable=True)
            ],

            # Create the semantic settings with the configuration
            semantic_search = SemanticSearch(configurations=[SemanticConfiguration(
                name="default",
                prioritized_fields=SemanticPrioritizedFields(
                    title_field=None,
                    content_fields=[SemanticField(field_name="content")]
                )
            )]),
                vector_search=VectorSearch(
                    algorithms=[
                        HnswAlgorithmConfiguration(
                            name="myHnsw",
                            kind=VectorSearchAlgorithmKind.HNSW,
                            parameters=HnswParameters(
                                m=4,
                                ef_construction=400,
                                ef_search=500,
                                metric=VectorSearchAlgorithmMetric.COSINE
                            )
                        ),
                        ExhaustiveKnnAlgorithmConfiguration(
                            name="myExhaustiveKnn",
                            kind=VectorSearchAlgorithmKind.EXHAUSTIVE_KNN,
                            parameters=ExhaustiveKnnParameters(
                                metric=VectorSearchAlgorithmMetric.COSINE
                            )
                        )
                    ],
                    profiles=[
                        VectorSearchProfile(
                            name="myHnswProfile",
                            algorithm_configuration_name="myHnsw",
                        ),
                        VectorSearchProfile(
                            name="myExhaustiveKnnProfile",
                            algorithm_configuration_name="myExhaustiveKnn",
                        )
                    ]

                )        
            )
        if args.verbose: print(f"Creating {args.index} search index")
        index_client.create_index(index)
    else:
        if args.verbose: print(f"Search index {args.index} already exists")

def index_sections(filename, sections):
    # if args.verbose: print(f"Indexing sections from '{filename}' into search index '{args.index}'")
    search_client = SearchClient(endpoint=f"https://{args.searchservice}.search.windows.net/",
                                    index_name=args.index,
                                    credential=search_creds)
    i = 0
    batch = []
    for s in sections:
        batch.append(s)
        i += 1
        if i % 1000 == 0:
            results = search_client.upload_documents(documents=batch)
            succeeded = sum([1 for r in results if r.succeeded])
            # if args.verbose: print(f"\tIndexed {len(results)} sections, {succeeded} succeeded")
            batch = []

    if len(batch) > 0:
        results = search_client.upload_documents(documents=batch)
        succeeded = sum([1 for r in results if r.succeeded])
        # if args.verbose: print(f"\tIndexed {len(results)} sections, {succeeded} succeeded")

def remove_from_index(filename):
    if args.verbose: print(f"Removing sections from '{filename or '<all>'}' from search index '{args.index}'")
    search_client = SearchClient(endpoint=f"https://{args.searchservice}.search.windows.net/",
                                    index_name=args.index,
                                    credential=search_creds)
    while True:
        filter = None if filename == None else f"sourcefile eq '{os.path.basename(filename)}'"
        r = search_client.search("", filter=filter, top=1000, include_total_count=True)
        if r.get_count() == 0:
            break
        r = search_client.delete_documents(documents=[{ "id": d["id"] } for d in r])
        if args.verbose: print(f"\tRemoved {len(r)} sections from index")
        # It can take a few seconds for search results to reflect changes, so wait a bit
        time.sleep(2)

  
def get_last_processed_file(log_file):  
    last_processed_file = None  
    with open(log_file, 'r') as f:  
        lines = f.readlines()  
        for line in reversed(lines):  
            if "Processing '" in line:  
                # Use regex to extract the file name  
                match = re.search(r"Processing '(.*?)' at progress", line)  
                if match:  
                    last_processed_file = match.group(1)  
                    break  
    return last_processed_file 

def process_file(filename, args, storage_creds, use_vectors):  
    # print(f"Processing '{filename}'")  
    # Download the blob to a temporary file  
    blob_service_client = BlobServiceClient(account_url=f"https://{args.storageaccount}.blob.core.windows.net", credential=storage_creds)  
    blob_client = blob_service_client.get_blob_client(container=args.container_source, blob=filename)  
  
    with tempfile.TemporaryDirectory() as temp_dir:  
        local_file_path = os.path.join(temp_dir, os.path.basename(filename))  
        with open(local_file_path, "wb") as download_file:  
            download_file.write(blob_client.download_blob().readall())  
  
        # Now the file is downloaded, we can process it  
        # if args.verbose:  
        #     print(f"Downloaded and processing '{local_file_path}'")  
  
        if args.remove:  
            remove_blobs(filename)  
            remove_from_index(filename)  
        elif args.removeall:  
            remove_blobs(None)  
            remove_from_index(None)  
        else:  
            if not args.skipblobs:  
                # try:  
                upload_blobs(local_file_path)  

                page_map = get_document_text(local_file_path)  
                sections = create_sections(os.path.basename(local_file_path), page_map, use_vectors)  
                index_sections(os.path.basename(local_file_path), sections)  
                # except Exception as e:  
                #     print("Failed to process file:", local_file_path)  
                #     print(e)  

if __name__ == "__main__":

    parser = argparse.ArgumentParser(
        description="Prepare documents by extracting content from PDFs, splitting content into sections, uploading to blob storage, and indexing in a search index.",
        epilog="Example: prepdocs.py '..\data\*' --storageaccount myaccount --container mycontainer --searchservice mysearch --index myindex -v"
        )
    parser.add_argument("files", help="Files to be processed")
    parser.add_argument("--product", help="Value for the product field in the search index for all sections indexed in this run")
    parser.add_argument("--skipblobs", action="store_true", help="Skip uploading individual pages to Azure Blob Storage")
    parser.add_argument("--storageaccount", help="Azure Blob Storage account name")
    parser.add_argument("--container", help="Azure Blob Storage container name")
    parser.add_argument("--container_source", help="Azure Blob Storage container name to load data from")
    parser.add_argument("--storagekey", required=False, help="Optional. Use this Azure Blob Storage account key instead of the current user identity to login (use az login to set current user for Azure)")
    parser.add_argument("--tenantid", required=False, help="Optional. Use this to define the Azure directory where to authenticate)")
    parser.add_argument("--searchservice", help="Name of the Azure Cognitive Search service where content should be indexed (must exist already)")
    parser.add_argument("--index", help="Name of the Azure Cognitive Search index where content should be indexed (will be created if it doesn't exist)")
    parser.add_argument("--searchkey", required=False, help="Optional. Use this Azure Cognitive Search account key instead of the current user identity to login (use az login to set current user for Azure)")
    parser.add_argument("--openaiservice", help="Name of the Azure OpenAI service used to compute embeddings")
    parser.add_argument("--openaideployment", help="Name of the Azure OpenAI model deployment for an embedding model ('text-embedding-ada-002' recommended)")
    parser.add_argument("--novectors", action="store_true", help="Don't compute embeddings for the sections (e.g. don't call the OpenAI embeddings API during indexing)")
    parser.add_argument("--openaikey", required=False, help="Optional. Use this Azure OpenAI account key instead of the current user identity to login (use az login to set current user for Azure)")
    parser.add_argument("--remove", action="store_true", help="Remove references to this document from blob storage and the search index")
    parser.add_argument("--removeall", action="store_true", help="Remove all blobs from blob storage and documents from the search index")
    parser.add_argument("--localpdfparser", action="store_true", help="Use PyPdf local PDF parser (supports only digital PDFs) instead of Azure Form Recognizer service to extract text, tables and layout from the documents")
    parser.add_argument("--formrecognizerservice", required=False, help="Optional. Name of the Azure Form Recognizer service which will be used to extract text, tables and layout from the documents (must exist already)")
    parser.add_argument("--formrecognizerkey", required=False, help="Optional. Use this Azure Form Recognizer account key instead of the current user identity to login (use az login to set current user for Azure)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()
    # Use the current user identity to connect to Azure services unless a key is explicitly set for any of them
    # azd_credential = AzureDeveloperCliCredential() if args.tenantid == None else AzureDeveloperCliCredential(tenant_id=args.tenantid, process_timeout=60)
    # default_creds = azd_credential if args.searchkey == None or args.storagekey == None else None
    # search_creds = default_creds if args.searchkey == None else AzureKeyCredential(args.searchkey)
    default_creds= None
    search_creds = default_creds if args.searchkey == None else AzureKeyCredential(args.searchkey)

    use_vectors = not args.novectors

    if not args.skipblobs:
        storage_creds = default_creds if args.storagekey == None else args.storagekey
    if not args.localpdfparser:
        # check if Azure Form Recognizer credentials are provided
        if args.formrecognizerservice == None:
            print("Error: Azure Form Recognizer service is not provided. Please provide formrecognizerservice or use --localpdfparser for local pypdf parser.")
            exit(1)
        formrecognizer_creds = default_creds if args.formrecognizerkey == None else AzureKeyCredential(args.formrecognizerkey)

    if use_vectors:
        client = AzureOpenAI(
        api_key=args.openaikey,  
        api_version="2023-12-01-preview",
        azure_endpoint = f"https://{args.openaiservice}.openai.azure.com"
        )


    if args.removeall:  
        remove_blobs(None)  
        remove_from_index(None)  
    else:  
        if not args.remove:  
            create_search_index()  
  
        print("Processing files...")  
        blob_names = list_blobs_recursively(storage_creds, args)  
        file_names = [filename for filename in blob_names if filename.lower().endswith('.pdf')]  
        print(f"There are {len(file_names)} pdf files")  
  
        # get the last processed file from the log 
        last_processed_file=None 
        if os.path.isfile('nohup.out'):
            last_processed_file = get_last_processed_file('nohup.out')  
        
        # start processing from the beginning or after the last processed file  
        if last_processed_file:  
            file_names = file_names[file_names.index(last_processed_file) + 1:]  
        # for filename in file_names:
        #     process_file(filename, args, storage_creds, use_vectors)
        # Process each blob if it is a PDF file using parallel processes  
        with ProcessPoolExecutor() as executor:
            futures = {executor.submit(process_file, filename, args, storage_creds, use_vectors): filename for filename in file_names}  
            # Counter for processed files  
            processed_files_count = 0  
            total_files = len(file_names)  
  
            for future in as_completed(futures):  
                # result = future.result()  
                processed_files_count += 1  
                print(f"Processed {processed_files_count} out of {total_files}")  
  
        print(f"Total {processed_files_count} files processed")