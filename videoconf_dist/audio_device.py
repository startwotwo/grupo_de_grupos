import sounddevice as sd

print("\n=== DISPOSITIVOS DISPONÍVEIS ===\n")
print(sd.query_devices())

print("\n=== SELEÇÃO ===\n")

input_id = int(input("ID do microfone (INPUT): "))
output_id = int(input("ID do fone (OUTPUT): "))

input_info = sd.query_devices(input_id)
output_info = sd.query_devices(output_id)

# 🔥 PADRÃO VOIP (não usar valores do hardware)
SAMPLE_RATE = 48000
CHANNELS_INPUT = 1
CHANNELS_OUTPUT = 2

print("\n=== RESULTADO DO HARDWARE ===\n")

print("INPUT:")
print(f"Nome: {input_info['name']}")
print(f"Max input channels: {input_info['max_input_channels']}")
print(f"Default sample rate: {input_info['default_samplerate']}")

print("\nOUTPUT:")
print(f"Nome: {output_info['name']}")
print(f"Max output channels: {output_info['max_output_channels']}")
print(f"Default sample rate: {output_info['default_samplerate']}")

print("\n=== CONFIG FINAL (USE ESSA) ===\n")

print(f"AUDIO_INPUT_DEVICE = {input_id}")
print(f"AUDIO_OUTPUT_DEVICE = {output_id}")
print(f"AUDIO_SAMPLE_RATE = {SAMPLE_RATE}")
print(f"AUDIO_CHANNELS = {CHANNELS_INPUT}")