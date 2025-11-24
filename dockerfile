FROM python:3
LABEL Maintainer ="objectmanip"

ENV TZ=Europe/Berlin
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /usr/app/src
# to add the remote file at root directory in container
COPY ./ ./
# CMD instructions used to run the software
RUN pip install -r ./requirements.txt

CMD ["python", "./main.py"]
