"""Test local: investigador completo con DeepSeek."""
import sys
sys.path.insert(0, r'C:\Users\mapos\Dropbox\Programas\agente_map')
from dotenv import load_dotenv
load_dotenv()
import config

from models.schemas import ProjectSession
import agents.researcher as researcher

print(f"ROLE_RESEARCH: {config.ROLE_RESEARCH}")
print(f"BUILDER_ROTATION: {config.BUILDER_ROTATION}")
print(f"DeepSeek key: {'OK' if config.DEEPSEEK_API_KEY else 'FALTA'}")
print(f"Mistral key: {'OK' if config.MISTRAL_API_KEY else 'FALTA'}")
print(f"Anthropic key: {'OK' if config.ANTHROPIC_API_KEY else 'FALTA'}")

session = ProjectSession(
    session_id='test_local_ds',
    user_input='gestion residuos solidos comunidades rurales Azuay Ecuador financiamiento no reembolsable',
    input_mode='search',
    doc_type_key='propuesta',
)

print('\nIniciando investigador con DEEPSEEK...')
import agents.researcher as researcher

# Forzar uso de deepseek como proveedor primario
import agents.llm as llm_mod
original_complete_builder = llm_mod.complete_builder

try:
    result = researcher.run(session, api_key=config.ANTHROPIC_API_KEY)
    print(f'\nSUCCESS!')
    print(f'viable={result.viable}, go_no_go={result.go_no_go}')
    print(f'viability_score={result.viability_score}')
    print(f'funder={result.funder.name}')
    print(f'Used providers: {session.builder_log}')
except Exception as e:
    import traceback
    print(f'\nERROR: {type(e).__name__}: {str(e)[:500]}')
    traceback.print_exc()
