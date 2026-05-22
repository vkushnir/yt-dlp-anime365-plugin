PLUGIN_NAME   := anime365
PLUGIN_SRC    := yt_dlp_plugins/extractor/anime365.py
PLUGIN_DIR    := $(HOME)/.config/yt-dlp/plugins/$(PLUGIN_NAME)/yt_dlp_plugins/extractor

.PHONY: install uninstall reinstall check clean

install: ## Установить плагин (копировать в ~/.config/yt-dlp/plugins/)
	mkdir -p $(PLUGIN_DIR)
	cp $(PLUGIN_SRC) $(PLUGIN_DIR)/anime365.py
	@echo "Установлено: $(PLUGIN_DIR)/anime365.py"

uninstall: ## Удалить плагин
	rm -rf $(HOME)/.config/yt-dlp/plugins/$(PLUGIN_NAME)
	@echo "Удалено."

reinstall: uninstall install ## Переустановить плагин

check: ## Проверить что плагин загружается
	yt-dlp -v 2>&1 | grep -i "anime365"

clean: ## Удалить .pyc и кэш
	find . -type f -name '*.pyc' -delete
	find . -type d -name '__pycache__' -exec rm -rf {} +

help: ## Показать список команд
	@grep -E '^[a-zA-Z_-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*##"}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
