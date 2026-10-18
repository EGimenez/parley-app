"""install_config — the interactive setup wizard (iterations 23-25).

`parley --setup [name]` builds a private agent cfg under ~/.parley by asking,
verifying live, and only then writing. Layout:

    install_config.py   orchestrator: name -> accounts -> policy -> LLM ->
                        atomic write + real-loader read-back; --add-account
    backends/           ONE module per account backend (gmail, outlook,
                        yahoo, local_outlook): menu label, guided collect()
                        flow, YAML item template — plus a registry
    steps.py            agent-level steps: LLM brain + gateway (pinged live),
                        the policy prose
    console.py          prompt primitives + the frontend seam
    setup_window.py     the GUI frontend (tkinter, always-on-top, screenshots)
"""
