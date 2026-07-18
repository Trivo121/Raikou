from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue, PayloadSchemaType
from app.core.config import settings
import uuid

class QdrantStore:
    _instance = None
    
    def __init__(self):
        self.client = QdrantClient(
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY
        )
        
    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def initialize_collection(self, collection_name: str, vector_size: int = 768):
        if not self.client.collection_exists(collection_name=collection_name):
            self.client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE)
            )
        self._ensure_payload_indexes(collection_name)


    def _ensure_payload_indexes(self, collection_name: str):
        try:
            self.client.create_payload_index(
                collection_name=collection_name,
                field_name="session_id",
                field_schema=PayloadSchemaType.KEYWORD,
                wait=True,
            )
        except Exception as e:
            message = str(e).lower()
            if "already exists" not in message and "already has" not in message:
                raise
    def upsert_vectors(self, collection_name: str, points: list[dict]):
        """
        points is a list of dicts: {"id": str, "vector": list[float], "payload": dict}
        """
        qdrant_points = [
            PointStruct(
                id=p["id"],
                vector=p["vector"],
                payload=p.get("payload", {})
            )
            for p in points
        ]
        self.client.upsert(
            collection_name=collection_name,
            points=qdrant_points
        )

    def search_vectors(self, collection_name: str, query_vector: list[float], limit: int = 10, session_id: str = None) -> list[dict]:
        """
        Search for nearest neighbors in the given collection.
        Returns a list of dicts with score, id, and payload.
        """
        query_filter = None
        if session_id:
            query_filter = Filter(
                must=[
                    FieldCondition(
                        key="session_id",
                        match=MatchValue(value=session_id)
                    )
                ]
            )

        search_result = self.client.query_points(
            collection_name=collection_name,
            query=query_vector,
            query_filter=query_filter,
            limit=limit
        )
        
        results = []
        for hit in getattr(search_result, "points", search_result):
            results.append({
                "id": str(hit.id),
                "score": hit.score,
                "payload": hit.payload
            })
            
        return results

    def count_vectors_by_session(self, collection_name: str, session_id: str) -> int:
        if not self.client.collection_exists(collection_name=collection_name):
            return 0
        try:
            count_result = self.client.count(
                collection_name=collection_name,
                count_filter=Filter(
                    must=[
                        FieldCondition(
                            key="session_id",
                            match=MatchValue(value=session_id)
                        )
                    ]
                )
            )
            return count_result.count
        except Exception:
            return 0

    def delete_vectors_by_session(self, collection_name: str, session_id: str):
        if not self.client.collection_exists(collection_name=collection_name):
            return
        self.client.delete(
            collection_name=collection_name,
            points_selector=Filter(
                must=[
                    FieldCondition(
                        key="session_id",
                        match=MatchValue(value=session_id)
                    )
                ]
            )
        )

