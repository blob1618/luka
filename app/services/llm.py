from typing import Dict, Any

class LLMService:
    @staticmethod
    async def process_text_expense(text: str) -> Dict[str, Any]:
        """
        Stub for processing colloquial text via LLM.
        Expected to extract: amount, category, description
        """
        # TODO: Integrate with OpenAI/Gemini structured outputs
        return {
            "amount": 100.0,
            "category": "comida",
            "description": "Mocked LLM extraction"
        }

    @staticmethod
    async def process_audio_expense(audio_bytes: bytes) -> Dict[str, Any]:
        """
        Stub for transcribing audio and then extracting data.
        """
        # TODO: Call Whisper API, then pass to LLM
        return {
            "amount": 200.0,
            "category": "transporte",
            "description": "Mocked Audio extraction"
        }

    @staticmethod
    async def process_image_receipt(image_bytes: bytes) -> Dict[str, Any]:
        """
        Stub for performing OCR on an image.
        """
        # TODO: Pass image to Vision model
        return {
            "amount": 500.0,
            "category": "ocio",
            "description": "Mocked OCR extraction"
        }
