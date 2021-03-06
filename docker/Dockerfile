# Copyright (C) 2016-2017 Bitergia
# GPLv3 License

FROM debian:8
MAINTAINER Luis Cañas-Díaz <lcanas@bitergia.com>, Alvaro del Castillo <acs@bitergia.com>

ENV DEBIAN_FRONTEND noninteractive
ENV DEPLOY_USER bitergia
ENV DEPLOY_USER_DIR /home/${DEPLOY_USER}
ENV SCRIPTS_DIR ${DEPLOY_USER_DIR}/scripts

# Initial user
RUN useradd bitergia --create-home --shell /bin/bash

# install dependencies
RUN apt-get update && \
    apt-get -y install --no-install-recommends \
        bash locales \
        git git-core \
        tree ccze g++ \
        psmisc \
        python python3 pep8 \
        python3-gdbm \
        python3-bs4 \
        python3-pip python3-dev \
        python-mysqldb \
        python3-redis \
        python3-yaml \
        gcc make libmysqlclient-dev mariadb-client \
        vim unzip curl wget sudo ssh\
        && \
    apt-get clean && \
    find /var/lib/apt/lists -type f -delete

# Not available as package in Debian 8 python3-myssqldb
RUN pip3 install mysqlclient

# perceval needs a newer version than Debian 8 - this breaks pip3 so install a newer version before
RUN pip3 install pip --upgrade
RUN pip3 install requests --upgrade
RUN apt-get -y remove python3-requests

# Needed a newer version of setuptools
RUN apt-get -y remove python3-setuptools python3-pkg-resources
RUN pip3 install setuptools

# solves issues with UNICODE and SH
RUN pip3 install sqlalchemy

# sortinghat needs pandas
# With 0.20 it does not work with current SH 0.3
RUN pip3 install pandas==0.19.2

# reports needs prettylotlib and elasticsearch_dsl
RUN pip3 install prettyplotlib
RUN pip3 install elasticsearch_dsl

RUN echo "${DEPLOY_USER}    ALL=NOPASSWD: ALL" >> /etc/sudoers

RUN mkdir -p /home/bitergia/logs ; chown -R bitergia:bitergia /home/bitergia/logs
VOLUME ["/home/bitergia/logs"]

ADD stage ${DEPLOY_USER_DIR}/stage
RUN chmod 755 ${DEPLOY_USER_DIR}/stage

RUN sed -i -e 's/# en_US.UTF-8 UTF-8/en_US.UTF-8 UTF-8/' /etc/locale.gen && \
    echo 'LANG="en_US.UTF-8"'>/etc/default/locale && \
    dpkg-reconfigure --frontend=noninteractive locales && \
    update-locale LANG=en_US.UTF-8

ADD cacert.pem ${DEPLOY_USER_DIR}/cacert.pem
RUN CERT_PATH=`python3 -m requests.certs` && \
    cat ${DEPLOY_USER_DIR}/cacert.pem >> ${CERT_PATH}

ENV LANG en_US.UTF-8
ENV LANGUAGE en_US:en
ENV LC_ALL en_US.UTF-8
ENV LANG C.UTF-8

USER ${DEPLOY_USER}
WORKDIR ${DEPLOY_USER_DIR}

CMD ${DEPLOY_USER_DIR}/stage
