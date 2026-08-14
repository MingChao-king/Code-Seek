# Terminal integration

The terminal does not use a second interactive Agent subclass. `run/mini.py` composes the same `AssistantAgent` used by Python bindings and benchmarks, shows a read-only ContextView snapshot before each prompt, and provides immediate `/` completion for `/compact`, `/auto`, `/ask`, `/approval`, `/limit`, `/memory`, and `/exit`. `/compress` remains a compatibility alias for `/compact`.

??? note "Terminal source"

    ```python
    --8<-- "src/minisweagent/run/mini.py"
    ```

{% include-markdown "../../_footer.md" %}
