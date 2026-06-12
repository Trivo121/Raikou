class QdrantStore:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port

    def upsert_vectors(self, collection_name: str, vectors: list):
        # Insert vectors into Qdrant database
        pass
