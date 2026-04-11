copy agent files to GPU
scp -J navarlu2@ptak.felk.cvut.cz   voice-agent/src/{agent.py,tools.py,config.py}   navarlu2@woska:/mnt/data_personal/navarlu2/work/Pepper/voice-agent/src/

ssh navarlu2@ptak.felk.cvut.cz

docker compose -f docker/docker-compose.yml restart session-manager
