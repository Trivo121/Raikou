from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue, PayloadSchemaType
from app.core.config import settings
from app.services.storage.payloads import QdrantPatchPayload, TENANT_QDRANT_PAYLOAD_FIELDS

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
        else:
            collection = self.client.get_collection(collection_name=collection_name)
            vectors = collection.config.params.vectors
            actual_size = getattr(vectors, "size", None)
            if actual_size is None and isinstance(vectors, dict):
                # Named-vector collections are not part of the V1 contract.
                raise RuntimeError("Qdrant collection must expose one unnamed 768-dimensional vector.")
            if actual_size != vector_size:
                raise RuntimeError(
                    f"Qdrant collection vector size is {actual_size}; expected {vector_size} for SARCLIP."
                )
        self._ensure_payload_indexes(collection_name)


    def _ensure_payload_indexes(self, collection_name: str):
        # ``session_id`` keeps the existing prototype flow working.  Tenant
        # fields are indexed now so V1 searches can be strictly scoped from the
        # first production scene onward.
        for field_name in ("session_id", *TENANT_QDRANT_PAYLOAD_FIELDS, "source_artifact_id"):
            try:
                self.client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field_name,
                    field_schema=PayloadSchemaType.KEYWORD,
                    wait=True,
                )
            except Exception as e:
                message = str(e).lower()
                if "already exists" not in message and "already has" not in message:
                    raise
    def upsert_vectors(self, collection_name: str, points: list[dict]):
        """
        Legacy session-scoped upsert used by the prototype pipeline.

        New V1 scene processing must call :meth:`upsert_scoped_vectors` so
        ownership and evidence metadata are validated before reaching Qdrant.
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

    def upsert_scoped_vectors(self, collection_name: str, points: list[dict]) -> None:
        """Upsert V1 patch vectors only after validating their tenant payload.

        ``QdrantPatchPayload`` is intentionally required here rather than
        trusting callers to manually copy a few filter fields. This prevents a
        future worker from creating vectors that cannot be safely scoped to an
        owner, project, and scene.
        """
        qdrant_points: list[PointStruct] = []
        for point in points:
            try:
                payload = QdrantPatchPayload.model_validate(point["payload"])
                point_id = point["id"]
                vector = point["vector"]
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("Invalid V1 Qdrant point; a complete scoped payload is required") from exc

            qdrant_points.append(
                PointStruct(
                    id=point_id,
                    vector=vector,
                    payload=payload.as_qdrant_payload(),
                )
            )

        if qdrant_points:
            self.client.upsert(collection_name=collection_name, points=qdrant_points)

    def search_vectors(
        self,
        collection_name: str,
        query_vector: list[float],
        limit: int = 10,
        session_id: str | None = None,
        owner_id: str | None = None,
        project_id: str | None = None,
        scene_id: str | None = None,
    ) -> list[dict]:
        """
        Search for nearest neighbors in the given collection.
        Returns a list of dicts with score, id, and payload.
        """
        tenant_values = {
            "owner_id": owner_id,
            "project_id": project_id,
            "scene_id": scene_id,
        }
        # Project scope is the minimum V1 semantic-search boundary. A scene
        # filter narrows that boundary, but must never be used without its
        # owner and project. Legacy session-only search remains available only
        # to the explicitly gated prototype router.
        if (owner_id is None) != (project_id is None):
            raise ValueError(
                "owner_id and project_id must be supplied together for tenant-scoped vector search"
            )
        if scene_id is not None and (owner_id is None or project_id is None):
            raise ValueError("scene_id requires owner_id and project_id for tenant-scoped vector search")

        filter_values = {
            "session_id": session_id,
            **tenant_values,
        }
        conditions = [
            FieldCondition(key=key, match=MatchValue(value=str(value)))
            for key, value in filter_values.items()
            if value is not None
        ]
        query_filter = Filter(must=conditions) if conditions else None

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

    def search_scoped_vectors(
        self,
        collection_name: str,
        query_vector: list[float],
        *,
        owner_id: str,
        project_id: str,
        scene_id: str | None = None,
        scene_ids: list[str] | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Search an owned project, optionally restricted to owned scenes.

        ``owner_id`` and ``project_id`` are always mandatory.  A single
        ``scene_id`` selects one scene; ``scene_ids`` is used only after the
        API has resolved metadata filters against PostgreSQL.  The Qdrant
        filter still repeats the full tenant boundary and is never replaced by
        cache data or a database-only check.
        """
        if scene_id is not None and scene_ids is not None:
            raise ValueError("Provide either scene_id or scene_ids, not both")
        if scene_ids is not None:
            allowed = [str(value) for value in scene_ids if str(value)]
            if not allowed:
                return []
            must = [
                FieldCondition(key="owner_id", match=MatchValue(value=str(owner_id))),
                FieldCondition(key="project_id", match=MatchValue(value=str(project_id))),
            ]
            # A Qdrant ``should`` group requires one matching scene condition
            # in addition to the non-optional owner/project ``must`` clauses.
            query_filter = Filter(
                must=must,
                should=[
                    FieldCondition(key="scene_id", match=MatchValue(value=value))
                    for value in allowed
                ],
            )
            search_result = self.client.query_points(
                collection_name=collection_name,
                query=query_vector,
                query_filter=query_filter,
                limit=limit,
            )
            return [
                {"id": str(hit.id), "score": hit.score, "payload": hit.payload}
                for hit in getattr(search_result, "points", search_result)
            ]
        return self.search_vectors(
            collection_name=collection_name,
            query_vector=query_vector,
            limit=limit,
            owner_id=owner_id,
            project_id=project_id,
            scene_id=scene_id,
        )

    def search_scene_vectors(
        self,
        collection_name: str,
        query_vector: list[float],
        *,
        owner_id: str,
        project_id: str,
        scene_id: str,
        limit: int = 10,
    ) -> list[dict]:
        """Search a single owned scene with non-optional tenant filters.

        New V1 routes should call this method rather than the legacy generic
        ``search_vectors`` method, which remains only for session migration
        compatibility.
        """
        return self.search_vectors(
            collection_name=collection_name,
            query_vector=query_vector,
            limit=limit,
            owner_id=owner_id,
            project_id=project_id,
            scene_id=scene_id,
        )

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

    def count_vectors_by_scene(
        self, collection_name: str, *, owner_id: str, project_id: str, scene_id: str
    ) -> int:
        if not self.client.collection_exists(collection_name=collection_name):
            return 0
        result = self.client.count(
            collection_name=collection_name,
            count_filter=Filter(
                must=[
                    FieldCondition(key="owner_id", match=MatchValue(value=str(owner_id))),
                    FieldCondition(key="project_id", match=MatchValue(value=str(project_id))),
                    FieldCondition(key="scene_id", match=MatchValue(value=str(scene_id))),
                ]
            ),
        )
        return int(result.count)

    def delete_vectors_by_scene(
        self, collection_name: str, *, owner_id: str, project_id: str, scene_id: str
    ) -> None:
        """Delete a complete tenant-scoped scene; never accept a partial filter."""
        if not self.client.collection_exists(collection_name=collection_name):
            return
        self.client.delete(
            collection_name=collection_name,
            points_selector=Filter(
                must=[
                    FieldCondition(key="owner_id", match=MatchValue(value=str(owner_id))),
                    FieldCondition(key="project_id", match=MatchValue(value=str(project_id))),
                    FieldCondition(key="scene_id", match=MatchValue(value=str(scene_id))),
                ]
            ),
        )

