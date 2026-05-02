"""
ARR Suite LLM - Embedding Generator (Phase A)

Uses Ollama nomic-embed-text to generate embeddings for text.
"""

import httpx
import json
from typing import List, Optional
import numpy as np

from src.config import settings


class EmbeddingGenerator:
    """
    Generate embeddings using Ollama nomic-embed-text model.
    
    Usage:
        generator = EmbeddingGenerator()
        embeddings = await generator.generate_embeddings(["text1", "text2"])
    """
    
    def __init__(self):
        """Initialize embedding generator."""
        self.endpoint = f"{settings.effective_ollama}/api/embeddings"
        self.model = settings.EMBEDDING_MODEL
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            headers={"Content-Type": "application/json"}
        )
    
    async def generate_embedding(self, text: str) -> List[float]:
        """
        Generate embedding for a single text.
        
        Args:
            text: Input text
            
        Returns:
            Embedding vector (list of floats)
        """
        payload = {
            "model": self.model,
            "prompt": text
        }
        
        try:
            response = await self.client.post(self.endpoint, json=payload)
            response.raise_for_status()
            
            data = response.json()
            return data.get("embedding", [])
        
        except httpx.RequestError as e:
            print(f"Embedding generation failed: {e}")
            return []
    
    async def generate_embeddings(self, texts: List[str]) -> List[List[float]]:
        """
        Generate embeddings for multiple texts.
        
        Args:
            texts: List of input texts
            
        Returns:
            List of embedding vectors
        """
        embeddings = []
        
        for text in texts:
            embedding = await self.generate_embedding(text)
            embeddings.append(embedding)
        
        return embeddings
    
    async def generate_media_embedding(
        self,
        title: str,
        genres: Optional[List[str]] = None,
        overview: Optional[str] = None
    ) -> List[float]:
        """
        Generate embedding for media item from multiple fields.
        
        Combines title, genres, and overview into a single embedding.
        
        Args:
            title: Media title
            genres: List of genres
            overview: Media overview/description
            
        Returns:
            Single embedding vector
        """
        # Combine fields
        text_parts = [title]
        
        if genres:
            text_parts.append(", ".join(genres))
        
        if overview:
            text_parts.append(overview)
        
        combined_text = " | ".join(text_parts)
        
        return await self.generate_embedding(combined_text)
    
    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()


# Example usage
async def main():
    """Demo embedding generation."""
    generator = EmbeddingGenerator()
    
    try:
        # Generate single embedding
        embedding = await generator.generate_embedding("Action movie with special effects")
        print(f"Single embedding dimension: {len(embedding)}")
        
        # Generate multiple embeddings
        texts = [
            "Science fiction movie about space travel",
            "Romantic comedy set in Paris",
            "Horror film in abandoned house"
        ]
        embeddings = await generator.generate_embeddings(texts)
        
        print(f"Generated {len(embeddings)} embeddings")
        print(f"First embedding dimension: {len(embeddings[0])}")
        
    finally:
        await generator.close()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
