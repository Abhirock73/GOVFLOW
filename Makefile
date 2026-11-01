.PHONY: dev test evals testbed demo clean help

help:
	@echo "GovFlow AI - Available Make Commands:"
	@echo "  make dev      - Start Backend & Frontend development servers"
	@echo "  make test     - Run unit & integration test suite"
	@echo "  make evals    - Run benchmark evaluation scenarios"
	@echo "  make testbed  - Run local mock test portals"
	@echo "  make demo     - Launch end-to-end demo"

dev:
	@echo "Starting GovFlow AI Backend & Frontend..."
	powershell -Command "Start-Process powershell -ArgumentList 'cd backend; $$env:PYTHONPATH=\".\"; ..\backend\.venv\Scripts\uvicorn.exe app.main:app --reload --port 8000'; cd frontend; npm run dev"

test:
	@echo "Running Pytest Test Suite..."
	powershell -Command "$$env:PYTHONPATH='backend'; .\backend\.venv\Scripts\python.exe -m pytest backend/tests -v"

evals:
	@echo "Running Benchmark Evaluation Scenarios..."
	powershell -Command "$$env:PYTHONPATH='backend'; .\backend\.venv\Scripts\python.exe evals/run_evals.py"

testbed:
	@echo "Starting Mock Test Portals (Scholarship :8081, License :8082, Subsidy :8083)..."
	powershell -Command "Start-Process powershell -ArgumentList 'cd testbed\scholarship; ..\..\backend\.venv\Scripts\python.exe app.py'; Start-Process powershell -ArgumentList 'cd testbed\license_renewal; ..\..\backend\.venv\Scripts\python.exe app.py'; cd testbed\subsidy; ..\..\backend\.venv\Scripts\python.exe app.py"

demo:
	@echo "Launching GovFlow AI Demo Suite..."
	powershell -Command "Start-Process powershell -ArgumentList 'cd testbed\scholarship; ..\..\backend\.venv\Scripts\python.exe app.py'; Start-Process powershell -ArgumentList 'cd backend; $$env:PYTHONPATH=\".\"; ..\backend\.venv\Scripts\uvicorn.exe app.main:app --port 8000'; cd frontend; npm run dev"

clean:
	@echo "Cleaning caches and temporary files..."
	powershell -Command "Remove-Item -Recurse -Force backend\.pytest_cache, backend\app\__pycache__ -ErrorAction SilentlyContinue"

