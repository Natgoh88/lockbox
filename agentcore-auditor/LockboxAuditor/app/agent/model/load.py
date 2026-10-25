from strands.models.bedrock import BedrockModel


def load_model() -> BedrockModel:
    """Get Bedrock model client using IAM credentials."""
    # Claude Opus 5.5 through the US cross-region profile, checked callable in this account in us-west-2.
    # Bedrock only: the deployed auditor never uses an Ollama or Anthropic API key.
    return BedrockModel(model_id="us.anthropic.claude-opus-5-5", region_name="us-west-2")
