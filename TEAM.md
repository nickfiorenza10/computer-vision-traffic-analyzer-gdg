# Roles & Responsibilities

| Role | Member | Owns |
|------|--------|------|
| Team Lead | Daksh Talati | Planning, coordination, final decisions |
| Backend | Monisha Natarajan | Rust API on Vercel (`api/`, `src/`) |
| Database | Sebastian Ward | Data storage, schema, queries |
| Full Stack | Michael McGrogan | Python pipeline (`pipeline/`) and end-to-end integration |
| Frontend | ChrisAnn Wright | React dashboard (`frontend/`) |
| Testing | Nicholas Fiorenza | Model training, evaluation, and accuracy testing |

## Team Lead: Daksh Talati

**Responsibilities**
- Set milestones and deadlines, assign tasks, and track progress
- Run team meetings and act as the main contact for GDG
- Make final calls on open decisions (congestion `[metric]`, accuracy target `±X%`)
- Review and approve pull requests into `main`

**Expected contributions**
- Project timeline and task board
- Final decisions on goals and scope, recorded in the README
- Demo and presentation coordination

## Backend: Monisha Natarajan

**Responsibilities**
- Own the Rust API that serves traffic data as JSON, deployed as a Vercel function (see `api/README.md`)
- Define the API endpoints with the frontend (e.g. counts per lane, volume per time bin)
- Connect the API to the data layer with the database owner

**Expected contributions**
- `api/` project with working REST endpoints
- API documentation (endpoint list with example requests and responses)
- Unit tests for the endpoints

## Database: Sebastian Ward

**Responsibilities**
- Own the `events.csv` schema and keep it in sync with the pipeline output
- Design the database (e.g. PostgreSQL) for when the data outgrows CSV files
- Write the queries that aggregate events into time bins and lane totals

**Expected contributions**
- Database schema and setup instructions
- Script to load `events.csv` into the database
- Aggregation queries used by the API

## Full Stack: Michael McGrogan

**Responsibilities**
- Build the Python detection and tracking pipeline (`detect.py`, `track_count.py`)
- Define lane polygons in `lanes.json` for each camera view
- Connect the pieces end to end: pipeline → data → API → dashboard
- Fill gaps between the backend and frontend as needed

**Expected contributions**
- Working pipeline that turns a video into `events.csv`
- Google Colab notebook for running the pipeline on a GPU
- End-to-end demo running on a sample video

## Frontend: ChrisAnn Wright

**Responsibilities**
- Build the React dashboard that displays data from the API
- Design the charts: volume over time, lane comparisons, congestion heatmap
- Agree on API response formats with the backend owner

**Expected contributions**
- `frontend/` React app with the main dashboard views
- Reusable chart components (Recharts or Plotly.js)
- Loading, empty, and error states for when the API has no data or is down

## Testing: Nicholas Fiorenza

**Responsibilities**
- Split labeled footage into training and test sets (70/30 or 80/20) and keep test data separate from training
- Fine-tune YOLO on our footage if the pretrained model underperforms, and compare results
- Measure detection quality (precision, recall, mAP) and counting accuracy against manual counts
- Compare trackers (ByteTrack vs. BoT-SORT) and test across conditions (day/night, rain, occlusion)

**Expected contributions**
- Labeled test dataset and documented train/test split
- Evaluation report with metrics for each model and tracker tried
- Recommendation for which model and tracker to use, and how close we are to the `±X%` target
- Trained model uploaded to Hugging Face

## Everyone

- Work on a branch and open a pull request; get at least one review before merging
- Keep your section of the README or docs up to date
- Flag blockers early in the team chat
