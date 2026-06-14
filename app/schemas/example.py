from typing import Any, Dict, Optional
from pydantic import BaseModel, Field

class EvaluationExample(BaseModel):
    id: str = Field(..., description="Unique identifier for the evaluation example")
    input: str = Field(..., description="The prompt or input text provided to the model")
    expected_output: str = Field(..., description="The ground truth or expected output from the model")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Optional metadata associated with the example")
